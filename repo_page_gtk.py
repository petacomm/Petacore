# --------------------------------------------------------------------------- #
# Repository — the APT archive: the pool, the index, and the copy on the server
# --------------------------------------------------------------------------- #
from . import repo  # noqa: E402

REPO_FILTERS = [("all", "repo_filter_all"),
                ("unindexed", "repo_filter_unindexed"),
                ("unsigned", "repo_filter_unsigned"),
                ("unpublished", "repo_filter_unpublished"),
                ("old", "repo_filter_old")]


def _ago(when: float) -> str:
    """A timestamp in the words a person would use."""
    if not when:
        return ""
    seconds = max(0, time.time() - when)
    if seconds < 90:
        return _("repo_just_now")
    if seconds < 3600:
        return _("repo_minutes_ago", n=int(seconds // 60))
    if seconds < 86400:
        return _("repo_hours_ago", n=int(seconds // 3600))
    return _("repo_days_ago", n=int(seconds // 86400))


class RepoPage(Gtk.Box):
    """Everything a personal APT archive needs, without a terminal.

    The page shows the pool as a list, because that is what the repository
    actually is. Three states are kept apart and shown separately, since
    confusing them is how an archive quietly stops working:

      * on disk   — the file is in the pool
      * indexed   — the signed index mentions it, so apt can see it
      * live      — the server is really serving it right now

    A package can be in the first and not the second (added but not rebuilt),
    or in the second and not the third (rebuilt but not published). Both are
    invisible in a file manager, which is why they are labelled here.
    """

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_top=12, margin_bottom=12,
                         margin_start=14, margin_end=14)
        self.window = window
        self._entries = []
        self._live = None          # {package: version} once the server is read
        self._live_error = ""
        self._undo = []            # removal records, newest last
        self._busy = False
        self._build()

    # -- construction ---------------------------------------------------------
    def _clear(self):
        child = self.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.remove(child)
            child = following

    def _build(self):
        self._clear()
        profile = repo.active_profile()
        if not profile:
            self._build_empty()
            return
        self.profile = profile
        self._build_toolbar()
        self._build_banner()
        self._build_list()
        self.refresh()

    def _build_empty(self):
        status = Adw.StatusPage(icon_name="package-x-generic-symbolic",
                                title=_("repo_none_title"),
                                description=_("repo_none_body"),
                                vexpand=True)
        button = Gtk.Button(label=_("repo_new"), halign=Gtk.Align.CENTER,
                            css_classes=["suggested-action", "pill"])
        button.connect("clicked", lambda *a: self._edit_profile(None))
        status.set_child(button)
        self.append(status)

    def _build_toolbar(self):
        bar = Gtk.Box(spacing=6)

        names = [p["name"] for p in repo.profiles()]
        self.repo_drop = Gtk.DropDown.new_from_strings(names)
        if self.profile["name"] in names:
            self.repo_drop.set_selected(names.index(self.profile["name"]))
        self.repo_drop.connect("notify::selected", self._on_repo_chosen, names)
        bar.append(self.repo_drop)

        settings = Gtk.Button(icon_name="emblem-system-symbolic",
                              tooltip_text=_("repo_settings"),
                              css_classes=["flat"])
        settings.connect("clicked",
                         lambda *a: self._edit_profile(self.profile))
        bar.append(settings)

        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)

        add = Gtk.Button(tooltip_text=_("repo_add"))
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                        label=_("repo_add")))
        add.connect("clicked", self._on_add_clicked)
        bar.append(add)

        self.build_btn = Gtk.Button(css_classes=["suggested-action"])
        self.build_btn.set_child(Adw.ButtonContent(
            icon_name="view-refresh-symbolic", label=_("repo_rebuild")))
        self.build_btn.connect("clicked", lambda *a: self._on_rebuild())
        bar.append(self.build_btn)

        self.publish_btn = Gtk.Button(tooltip_text=_("repo_publish"))
        self.publish_btn.set_child(Adw.ButtonContent(
            icon_name="send-to-symbolic", label=_("repo_publish")))
        self.publish_btn.connect("clicked", lambda *a: self._on_publish())
        bar.append(self.publish_btn)

        self.live_btn = Gtk.Button(icon_name="network-transmit-receive-symbolic",
                                   tooltip_text=_("repo_check_live"),
                                   css_classes=["flat"])
        self.live_btn.connect("clicked", lambda *a: self._on_check_live())
        bar.append(self.live_btn)

        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic",
                                  css_classes=["flat"])
        menu_btn.set_popover(self._page_menu())
        bar.append(menu_btn)
        self.append(bar)

        # One line of plain facts: how much is here, how old the index is,
        # whether it is signed, and what the server is actually serving.
        self.summary = Gtk.Label(xalign=0, wrap=True,
                                 css_classes=["dim-label", "caption"])
        self.append(self.summary)

    def _page_menu(self):
        popover = Gtk.Popover(has_arrow=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)

        def item(label, callback, destructive=False):
            classes = ["flat"] + (["destructive-action"] if destructive else [])
            button = Gtk.Button(label=label, css_classes=classes)
            button.get_child().set_xalign(0)
            button.connect("clicked",
                           lambda *a: (popover.popdown(), callback()))
            box.append(button)
            return button

        item(_("repo_instructions"), self._copy_instructions)
        item(_("repo_export_keyring"), self._export_keyring)
        item(_("repo_open_root"), self._open_root)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        item(_("repo_prune"), self._on_prune)
        self.trash_item = item(_("repo_empty_trash"), self._on_empty_trash,
                               destructive=True)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        item(_("repo_new"), lambda: self._edit_profile(None))
        item(_("repo_forget"), self._on_forget, destructive=True)

        popover.set_child(box)
        return popover

    def _build_banner(self):
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", lambda *a: self._on_rebuild())
        self.append(self.banner)

    def _build_list(self):
        search_row = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=_("repo_search"),
                                      hexpand=True)
        self.search.connect("search-changed", lambda *a: self._render())
        search_row.append(self.search)

        self.filter_drop = Gtk.DropDown.new_from_strings(
            [_(key) for _c, key in REPO_FILTERS])
        self.filter_drop.connect("notify::selected", lambda *a: self._render())
        search_row.append(self.filter_drop)

        self.undo_btn = Gtk.Button(icon_name="edit-undo-symbolic",
                                   tooltip_text=_("repo_undo") + " (Ctrl+Z)",
                                   css_classes=["flat"], sensitive=False)
        self.undo_btn.connect("clicked", lambda *a: self.undo_last())
        search_row.append(self.undo_btn)
        self.append(search_row)

        self.listbox = Gtk.ListBox(css_classes=["boxed-list"],
                                   selection_mode=Gtk.SelectionMode.MULTIPLE,
                                   valign=Gtk.Align.START)
        self.listbox.set_activate_on_single_click(False)
        self.listbox.connect("row-activated", self._on_row_activated)

        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        wrap.append(self.listbox)
        self.append(_scrolled(wrap))

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.listbox.add_controller(keys)

        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_right_click)
        self.listbox.add_controller(click)
        self.row_menu = Gtk.Popover(has_arrow=True)
        self.row_menu.set_parent(wrap)

        # Dropping .deb files anywhere on the page adds them to the pool.
        # This is the shortest path from "I just built something" to "it is
        # in the archive", and it is the same gesture the Project page uses.
        from gi.repository import Gdk
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        self.append(self.progress)

    # -- reading the repository ------------------------------------------------
    def refresh(self):
        if not getattr(self, "profile", None):
            return
        profile = self.profile

        def work():
            return repo.scan(profile), repo.status(profile)

        def done(result, error):
            if error or not result:
                self._entries, self._status = [], {}
                self._render()
                return
            self._entries, self._status = result
            self._render()
            self._render_summary()

        run_async(work, done)

    def _render_summary(self):
        state = getattr(self, "_status", {}) or {}
        parts = [_("repo_count", n=state.get("packages", 0)),
                 human_size(state.get("size", 0))]

        built = state.get("built", 0)
        parts.append(_("repo_index_built", t=_ago(built)) if built
                     else _("repo_index_never"))
        parts.append(_("repo_index_signed") if state.get("signed_index")
                     else _("repo_index_unsigned"))

        if self._live_error:
            parts.append(_("repo_live_failed", e=self._live_error))
        elif self._live is not None:
            parts.append(_("repo_live_count", n=len(self._live)))
        else:
            parts.append(_("repo_live_unknown"))

        self.summary.set_text("  ·  ".join(p for p in parts if p))

        if hasattr(self, "trash_item"):
            self.trash_item.set_label(
                f'{_("repo_empty_trash")}  ({state.get("trash", 0)})'
                if state.get("trash") else _("repo_empty_trash"))

        # The banner carries one problem at a time, in the order that stops
        # the archive working: no tool, no key, then an index left behind.
        if not repo.can_index():
            self._banner(_("repo_no_index_tool"), "")
        elif state.get("key") and not state.get("key_present"):
            self._banner(_("repo_key_missing"), "")
        elif state.get("stale"):
            self._banner(f'{_("repo_stale")} — {_("repo_stale_body")}',
                         _("repo_rebuild"))
        else:
            self.banner.set_revealed(False)

    def _banner(self, title, button_label):
        self.banner.set_title(title)
        self.banner.set_button_label(button_label or "")
        self.banner.set_revealed(True)

    # -- the list --------------------------------------------------------------
    def _live_state(self, entry):
        """Where this package stands against the copy on the server."""
        if self._live is None:
            return ""
        live = self._live.get(entry["package"])
        if not live:
            return "new"
        if live == entry["version"]:
            return "live"
        return "ahead" if repo.newer(entry["version"], live) else "behind"

    def _visible_entries(self):
        needle = self.search.get_text().strip().lower()
        mode = REPO_FILTERS[self.filter_drop.get_selected()][0]
        newest = {name: versions[0]["path"] for name, versions
                  in repo.group_by_package(self._entries).items()}

        shown = []
        for entry in self._entries:
            if needle and needle not in (entry["package"] + " "
                                         + entry["version"] + " "
                                         + entry["file"]).lower():
                continue
            if mode == "unindexed" and entry["indexed"]:
                continue
            if mode == "unsigned" and entry["signed"]:
                continue
            if mode == "unpublished" and self._live_state(entry) in ("live",
                                                                     ""):
                continue
            if mode == "old" and newest.get(entry["package"]) == entry["path"]:
                continue
            shown.append(entry)
        return shown

    def _render(self):
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)

        shown = self._visible_entries()
        if not shown:
            empty = Adw.ActionRow(title=_("repo_no_packages"),
                                  subtitle=_("repo_drop_hint"),
                                  subtitle_lines=2)
            empty.add_prefix(Gtk.Image(icon_name="package-x-generic-symbolic"))
            self.listbox.append(empty)
            return

        for entry in shown:
            self.listbox.append(self._row(entry))

    def _row(self, entry):
        title = f'{entry["package"]}  {entry["version"]}'.strip()
        details = [entry["arch"] or "—", human_size(entry["size"])]
        if entry["summary"]:
            details.append(entry["summary"])
        row = Adw.ActionRow(title=title, subtitle="  ·  ".join(details) + "\n"
                            + entry["rel"], subtitle_lines=2)
        row._entry = entry
        row.add_prefix(Gtk.Image(icon_name="package-x-generic-symbolic"))

        for label, css in self._badges(entry):
            row.add_suffix(Gtk.Label(label=label, valign=Gtk.Align.CENTER,
                                     css_classes=["caption"] + css))

        menu = Gtk.MenuButton(icon_name="view-more-symbolic",
                              valign=Gtk.Align.CENTER,
                              css_classes=["flat"])
        menu.set_popover(self._row_popover([entry]))
        row.add_suffix(menu)

        self._attach_drag(row, entry)
        return row

    def _badges(self, entry):
        badges = []
        if entry["broken"]:
            return [(_("repo_badge_broken"), ["error"])]
        if not entry["indexed"]:
            badges.append((_("repo_badge_unindexed"), ["warning"]))
        if not entry["signed"]:
            badges.append((_("repo_badge_unsigned"), ["dim-label"]))
        state = self._live_state(entry)
        if state == "live":
            badges.append((_("repo_badge_live"), ["success"]))
        elif state == "new":
            badges.append((_("repo_badge_new"), ["accent"]))
        elif state == "ahead":
            badges.append((_("repo_badge_ahead"), ["accent"]))
        elif state == "behind":
            badges.append((_("repo_badge_behind"), ["warning"]))
        return badges

    def _attach_drag(self, row, entry):
        """Let a package be dragged out to a file manager.

        Dragging in adds; dragging out takes a copy. Neither changes the
        archive behind the user's back — the file is copied, never moved.
        """
        from gi.repository import Gdk
        source = Gtk.DragSource(actions=Gdk.DragAction.COPY)

        def prepare(_source, _x, _y):
            try:
                return Gdk.ContentProvider.new_for_value(
                    Gio.File.new_for_path(entry["path"]))
            except Exception:
                return None

        source.connect("prepare", prepare)
        row.add_controller(source)

    # -- selection helpers -----------------------------------------------------
    def _selected(self):
        chosen = [getattr(r, "_entry", None)
                  for r in self.listbox.get_selected_rows()]
        return [e for e in chosen if e]

    def _on_row_activated(self, _listbox, row):
        entry = getattr(row, "_entry", None)
        if entry:
            self._show_details(entry)

    def _on_key(self, _controller, keyval, _code, state):
        from gi.repository import Gdk
        name = (Gdk.keyval_name(keyval) or "").lower()
        if name == "delete":
            self._on_remove(self._selected())
            return True
        if name == "f2":
            chosen = self._selected()
            if len(chosen) == 1:
                self._rename(chosen[0])
            return True
        if name == "z" and (state & Gdk.ModifierType.CONTROL_MASK):
            self.undo_last()
            return True
        return False

    def _on_right_click(self, _gesture, _n, x, y):
        from gi.repository import Gdk
        row = self.listbox.get_row_at_y(int(y))
        if row is None or not getattr(row, "_entry", None):
            return
        if row not in self.listbox.get_selected_rows():
            self.listbox.unselect_all()
            self.listbox.select_row(row)
        chosen = self._selected() or [row._entry]
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.row_menu.set_pointing_to(rect)
        self.row_menu.set_child(self._menu_box(chosen, self.row_menu))
        self.row_menu.popup()

    def _row_popover(self, entries):
        popover = Gtk.Popover(has_arrow=True)
        popover.set_child(self._menu_box(entries, popover))
        return popover

    def _menu_box(self, entries, popover):
        """The actions for a package, shared by the row button and the
        right-click menu so both offer exactly the same thing."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)

        def item(label, callback, destructive=False):
            classes = ["flat"] + (["destructive-action"] if destructive else [])
            button = Gtk.Button(label=label, css_classes=classes)
            button.get_child().set_xalign(0)
            button.connect("clicked",
                           lambda *a: (popover.popdown(), callback()))
            box.append(button)

        single = entries[0] if len(entries) == 1 else None
        if single:
            item(_("repo_details"), lambda: self._show_details(single))
            item(_("repo_rename"), lambda: self._rename(single))
            item(_("repo_copy_install"),
                 lambda: self._copy(repo.install_command(single["package"])))
            item(_("repo_copy_path"), lambda: self._copy(single["path"]))
            item(_("repo_verify"), lambda: self._verify(single))
            item(_("repo_open_folder"),
                 lambda: self._open(os.path.dirname(single["path"])))
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        item(_("repo_remove"), lambda: self._on_remove(entries),
             destructive=True)
        return box

    # -- adding ----------------------------------------------------------------
    def _on_add_clicked(self, _button):
        dialog = Gtk.FileDialog(title=_("repo_add"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        deb_filter = Gtk.FileFilter(name="Debian package (*.deb)")
        deb_filter.add_pattern("*.deb")
        filters.append(deb_filter)
        dialog.set_filters(filters)
        dialog.open_multiple(self.window, None, self._add_chosen)

    def _add_chosen(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        paths = []
        for index in range(files.get_n_items()):
            gfile = files.get_item(index)
            if gfile and gfile.get_path():
                paths.append(gfile.get_path())
        self.add_paths(paths)

    def _on_drop(self, _target, value, _x, _y):
        try:
            files = value.get_files()
        except Exception:
            return False
        paths = [f.get_path() for f in files
                 if f.get_path() and f.get_path().endswith(".deb")]
        if not paths:
            return False
        self.add_paths(paths)
        return True

    def add_paths(self, paths):
        """Copy packages into the pool. Used by the button, drag and drop,
        and by the Package page when a build finishes."""
        if not paths or not getattr(self, "profile", None):
            return
        profile = self.profile

        def done(result, error):
            if error:
                self.window.toast(str(error))
                return
            added, failures = result
            if added:
                self.window.toast(_("repo_added", n=len(added)))
            if failures:
                self.window.toast(_("repo_add_failed", n=len(failures))
                                  + f" — {failures[0][1]}")
            self.refresh()

        run_async(lambda: repo.add_packages(profile, paths), done)

    # -- removing --------------------------------------------------------------
    def _on_remove(self, entries):
        if not entries:
            return
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_remove_q", n=len(entries)),
                                   body=_("repo_remove_detail"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("remove", _("repo_remove"))
        dialog.set_response_appearance("remove",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response != "remove":
                return
            profile = self.profile
            paths = [e["path"] for e in entries]

            def done(record, error):
                if error:
                    self.window.toast(str(error))
                    return
                self._undo.append(record)
                del self._undo[:-10]
                self.undo_btn.set_sensitive(True)
                toast = Adw.Toast(title=_("repo_removed",
                                          n=len(record["items"])), timeout=6)
                toast.set_button_label(_("repo_undo"))
                toast.connect("button-clicked", lambda *a: self.undo_last())
                self.window.toaster.add_toast(toast)
                self.refresh()

            run_async(lambda: repo.remove_packages(profile, paths), done)

        dialog.connect("response", responded)
        dialog.present()

    def undo_last(self):
        if not self._undo:
            return
        record = self._undo.pop()
        self.undo_btn.set_sensitive(bool(self._undo))

        def done(restored, error):
            self.window.toast(str(error) if error
                              else _("repo_restored", n=len(restored or [])))
            self.refresh()

        run_async(lambda: repo.restore_removed(record), done)

    # -- renaming --------------------------------------------------------------
    def _rename(self, entry):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_rename_q"),
                                   body=_("repo_rename_hint"))
        field = Gtk.Entry(text=entry["file"], activates_default=True,
                          margin_top=6)
        dialog.set_extra_child(field)
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("rename", _("repo_rename"))
        dialog.set_response_appearance("rename",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")

        def responded(_dialog, response):
            if response != "rename":
                return
            new_name = field.get_text().strip()
            if not new_name or new_name == entry["file"]:
                return
            try:
                path = repo.rename_package(entry["path"], new_name)
            except (repo.RepoError, OSError) as e:
                self.window.toast(str(e))
                return
            self.window.toast(_("repo_renamed", n=os.path.basename(path)))
            self.refresh()

        dialog.connect("response", responded)
        dialog.present()

    # -- details, verification, clipboard --------------------------------------
    def _show_details(self, entry):
        def work():
            try:
                fields = repo.package_fields(entry["path"])
            except repo.RepoError as e:
                fields = {"Error": str(e)}
            return fields

        def done(fields, _error):
            lines = [f'{key}: {value}' for key, value in (fields or {}).items()
                     if key != "Description"]
            lines.append("")
            lines.append(f'{_("repo_copy_path")}: {entry["path"]}')
            body = "\n".join(lines)

            dialog = Adw.MessageDialog(transient_for=self.window,
                                       heading=_("repo_details"))
            label = Gtk.Label(label=body, xalign=0, selectable=True,
                              wrap=True, css_classes=["monospace", "caption"])
            scroller = Gtk.ScrolledWindow(child=label, min_content_height=260,
                                          propagate_natural_height=True)
            dialog.set_extra_child(scroller)
            dialog.add_response("close", _("close"))
            dialog.present()

        run_async(work, done)

    def _verify(self, entry):
        self.window.begin_operation(_("repo_verify"))

        def done(valid, error):
            self.window.end_operation()
            if error:
                self.window.toast(str(error))
                return
            self.window.toast(_("repo_verify_ok") if valid
                              else _("repo_verify_bad"))

        run_async(lambda: repo.verify(entry["path"]), done)

    def _copy(self, text):
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(text)
            self.window.toast(_("repo_copied"))

    def _copy_instructions(self):
        self._copy(repo.install_instructions(self.profile))

    def _open(self, path):
        Gio.AppInfo.launch_default_for_uri(f"file://{path}", None)

    def _open_root(self):
        self._open(self.profile["root"])

    def _export_keyring(self):
        profile = self.profile
        if not profile["key"]:
            self.window.toast(_("repo_key_hint"))
            return

        def done(path, error):
            self.window.toast(str(error) if error
                              else _("pubkey_done", p=path))

        run_async(lambda: repo.export_key(profile["root"], profile["key"],
                                          repo.keyring_filename(profile)),
                  done)

    # -- the index -------------------------------------------------------------
    def _on_rebuild(self):
        profile = self.profile
        if not profile["key"]:
            self.window.toast(_("repo_key_hint"))
            self._edit_profile(profile)
            return
        self.build_btn.set_sensitive(False)
        self.window.begin_operation(_("repo_building"))

        def done(_result, error):
            self.window.end_operation()
            self.build_btn.set_sensitive(True)
            self.window.toast(_("repo_build_failed", e=str(error)) if error
                              else _("repo_built"))
            self.refresh()

        run_async(lambda: repo.build(profile), done)

    # -- publishing ------------------------------------------------------------
    def _on_publish(self):
        profile = self.profile
        if profile["publish_method"] == "none":
            self._edit_profile(profile)
            return
        self.publish_btn.set_sensitive(False)

        def done(changes, error):
            self.publish_btn.set_sensitive(True)
            if error:
                self.window.toast(_("repo_publish_failed", e=str(error)))
                return
            if not changes:
                self.window.toast(_("repo_publish_none"))
                return
            self._confirm_publish(changes)

        run_async(lambda: repo.publish_preview(profile), done)

    def _confirm_publish(self, changes):
        preview = "\n".join(changes[:14])
        if len(changes) > 14:
            preview += f"\n… {len(changes) - 14}"
        dialog = Adw.MessageDialog(
            transient_for=self.window,
            heading=_("repo_publish_q", n=len(changes)),
            body=f'{self.profile["publish_target"]}\n\n'
                 f'{_("repo_publish_detail")}')
        label = Gtk.Label(label=preview, xalign=0, selectable=True,
                          css_classes=["monospace", "caption"])
        dialog.set_extra_child(Gtk.ScrolledWindow(
            child=label, min_content_height=160,
            propagate_natural_height=True))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("publish", _("repo_publish"))
        dialog.set_response_appearance("publish",
                                       Adw.ResponseAppearance.SUGGESTED)

        def responded(_dialog, response):
            if response != "publish":
                return
            profile = self.profile
            self.publish_btn.set_sensitive(False)
            self.window.begin_operation(_("repo_publishing"))

            def done(result, error):
                self.window.end_operation()
                self.publish_btn.set_sensitive(True)
                if error:
                    self.window.toast(_("repo_publish_failed", e=str(error)))
                    return
                self.window.toast(_("repo_published",
                                    n=(result or {}).get("files", 0)))
                self._on_check_live()

            run_async(lambda: repo.publish(profile), done)

        dialog.connect("response", responded)
        dialog.present()

    # -- the copy on the server ------------------------------------------------
    def _on_check_live(self):
        profile = self.profile
        if not profile["base_url"]:
            self._edit_profile(profile)
            return
        self.live_btn.set_sensitive(False)

        def done(result, error):
            self.live_btn.set_sensitive(True)
            if error:
                self._live, self._live_error = None, str(error)
            else:
                self._live = {name: info["version"]
                              for name, info in (result or {}).items()}
                self._live_error = ""
            self._render()
            self._render_summary()

        run_async(lambda: repo.remote_packages(profile), done)

    # -- housekeeping ----------------------------------------------------------
    def _on_prune(self):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_prune_q"),
                                   body=_("repo_prune_detail"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("prune", _("repo_prune"))
        dialog.set_response_appearance("prune",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response != "prune":
                return
            profile = self.profile

            def done(record, error):
                if error:
                    self.window.toast(str(error))
                    return
                if record["items"]:
                    self._undo.append(record)
                    self.undo_btn.set_sensitive(True)
                self.window.toast(_("repo_removed", n=len(record["items"])))
                self.refresh()

            run_async(lambda: repo.prune(profile, keep=1), done)

        dialog.connect("response", responded)
        dialog.present()

    def _on_empty_trash(self):
        count = (getattr(self, "_status", {}) or {}).get("trash", 0)
        if not count:
            return
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_empty_trash_q", n=count),
                                   body=self.profile["root"])
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("empty", _("repo_empty_trash"))
        dialog.set_response_appearance("empty",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response != "empty":
                return
            profile = self.profile
            # Emptying the trash is the one thing here that cannot be undone,
            # so the undo stack is dropped with it rather than left pointing
            # at files that are gone.
            self._undo.clear()
            self.undo_btn.set_sensitive(False)

            def done(removed, error):
                self.window.toast(str(error) if error
                                  else _("repo_trash_emptied", n=removed or 0))
                self.refresh()

            run_async(lambda: repo.empty_trash(profile), done)

        dialog.connect("response", responded)
        dialog.present()

    def _on_forget(self):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_forget_q"),
                                   body=_("repo_forget_detail"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("forget", _("repo_forget"))
        dialog.set_response_appearance("forget",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response == "forget":
                repo.remove_profile(self.profile["name"])
                self._live, self._live_error = None, ""
                self._build()

        dialog.connect("response", responded)
        dialog.present()

    # -- profiles --------------------------------------------------------------
    def _on_repo_chosen(self, drop, _pspec, names):
        index = drop.get_selected()
        if 0 <= index < len(names) and names[index] != self.profile["name"]:
            repo.set_active(names[index])
            self._live, self._live_error = None, ""
            self._undo.clear()
            self._build()

    def _edit_profile(self, profile):
        from .dialogs import RepoSettingsDialog

        def saved(_stored):
            self._live, self._live_error = None, ""
            self._build()
            self.window.toast(_("repo_created"))

        RepoSettingsDialog(self.window, profile, on_saved=saved).present()
