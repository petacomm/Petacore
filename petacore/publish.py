"""Publishing a project to GitHub as a new release, in one go.

    "1.0 Beta 3"  →  code committed and pushed to main
                  →  a GitHub release "1.0 Beta 3", tag v1.0-beta.3,
                     marked as a pre-release
                  →  the .deb / .rpm of that version from the version
                     library attached to it

The person picks or types a version; everything else is derived from it.
Pre-release numbers may have a dot ("Beta 2.1") — a quick fix to a beta
should not have to pretend to be the next beta.

Nothing is forced: what is already on GitHub is kept, and a version that is
already published is refused rather than overwritten.
"""

import re

from . import github, gitops, versions

CHANNELS = ("alpha", "beta", "rc", "stable")
TITLES = {"alpha": "Alpha", "beta": "Beta", "rc": "RC"}


class PublishError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Version names
# --------------------------------------------------------------------------- #
def _channel_words():
    """Every way a person might write a channel name: English, the short
    forms, and the name in each of Petacore's languages."""
    words = {"alpha": "alpha", "a": "alpha", "beta": "beta", "b": "beta",
             "rc": "rc", "pre": "rc", "candidate": "rc"}
    try:
        from .i18n import _S
        for channel in ("alpha", "beta", "rc"):
            for text in _S.get(f"ver_ch_{channel}", {}).values():
                words[text.lower()] = channel
    except Exception:            # noqa: BLE001 - parsing must not need i18n
        pass
    return words


_SHAPE = re.compile(
    r"^\s*v?(\d+(?:\.\d+){0,3})"            # 1.0
    r"(?:\s*[-_ ]?\s*([^\W\d_]+)"           # beta / Бета / rc
    r"\s*[-_. ]?\s*(\d+(?:\.\d+)?)?)?"      # 2 / 2.1
    r"\s*$", re.UNICODE)


def parse(text, number_hint=""):
    """"1.0 Beta 2.1" → ("1.0", "beta", "2.1"); "v2.0" → ("2.0", "stable",
    ""). With a hint, "beta 3" alone means that number's Beta 3. Raises
    PublishError for anything else."""
    text = (text or "").strip()
    if number_hint and text[:1].isalpha() and text[:1] not in "vV":
        text = f"{number_hint} {text}"
    match = _SHAPE.match(text)
    if not match:
        raise PublishError("bad-version")
    number, word, pre = match.groups()
    if not word:
        return number, "stable", ""
    channel = _channel_words().get(word.lower())
    if not channel:
        raise PublishError("bad-version")
    pre = pre or "1"
    if pre.split(".")[0] == "0" and "." not in pre:
        raise PublishError("bad-version")
    return number, channel, pre


def title(number, channel, pre):
    """The release's name on GitHub — in English, because a release page is
    read by everyone: "1.0 Beta 2.1"."""
    if channel == "stable":
        return number
    return f"{number} {TITLES[channel]} {pre}"


def tag(number, channel, pre):
    """A semantic-version tag: v1.0, v1.0-beta.2, v1.0-beta.2.1."""
    if channel == "stable":
        return f"v{number}"
    return f"v{number}-{channel}.{pre}"


def from_tag(text):
    """The reverse of tag(), forgiving about how other tools wrote it;
    None for a tag that is not a version."""
    try:
        return parse((text or "").replace("-", " ").replace("_", " "))
    except PublishError:
        return None


def _key(number, channel, pre):
    nums = tuple(int(p) for p in number.split("."))
    nums += (0,) * (4 - len(nums))
    pres = tuple(int(p) for p in pre.split(".")) if pre else ()
    return nums + (CHANNELS.index(channel),) + pres


def suggestions(known=()):
    """What a person most likely publishes next, best guess first.

    `known` holds versions already out there (GitHub releases, the version
    library), each as (number, channel, pre). After 1.0 Beta 2 that is
    Beta 3, a quick Beta 2.1, the release candidate and 1.0 itself; after
    1.0 it is 1.0.1, 1.1 and 2.0.
    """
    known = sorted({k for k in known if k}, key=lambda k: _key(*k),
                   reverse=True)
    if not known:
        return [("1.0", "beta", "1"), ("1.0", "stable", "")]
    number, channel, pre = known[0]
    if channel == "stable":
        parts = [int(p) for p in number.split(".")] + [0, 0]
        major, minor, patch = parts[0], parts[1], parts[2]
        out = [(f"{major}.{minor}.{patch + 1}", "stable", ""),
               (f"{major}.{minor + 1}", "stable", ""),
               (f"{major}.{minor + 1}", "beta", "1"),
               (f"{major + 1}.0", "stable", "")]
    else:
        main = pre.split(".")[0]
        sub = pre.split(".")[1] if "." in pre else "0"
        out = [(number, channel, str(int(main) + 1)),
               (number, channel, f"{main}.{int(sub) + 1}")]
        if channel != "rc":
            out.append((number, "rc", "1"))
        out.append((number, "stable", ""))
    taken = {_key(*k) for k in known}
    unique = []
    for item in out:
        if _key(*item) not in taken and item not in unique:
            unique.append(item)
    return unique


def suggest_for(library=(), released=()):
    """Suggestions for the publish dialog.

    A version built into the library but not yet on GitHub comes first —
    the package is made, it only has to go out. Then whatever would come
    after everything known.
    """
    released = [r for r in released if r]
    newest = max((_key(*r) for r in released), default=None)
    waiting = sorted({v for v in library if v and v not in released
                      and (newest is None or _key(*v) > newest)},
                     key=lambda v: _key(*v), reverse=True)[:2]
    after = suggestions(list(library) + released)
    return (waiting + [s for s in after if s not in waiting])[:5]


def library_versions(project_path):
    """The version library's releases, as (number, channel, pre)."""
    return [(v["number"], v["channel"], str(v["pre"]) if v["pre"] else "")
            for v in versions.list_versions(project_path)]


def packages_for(project_path, number, channel, pre):
    """The .deb / .rpm files the version library holds for this version."""
    for v in versions.list_versions(project_path):
        if (v["number"] == number and v["channel"] == channel
                and (str(v["pre"]) if v["pre"] else "") == pre):
            return [f["path"] for f in v["files"]]
    return []


# --------------------------------------------------------------------------- #
# Where the project goes
# --------------------------------------------------------------------------- #
def target(project_path):
    """(owner, repo) of the project's GitHub repository; None when it has
    no remote yet. Raises when the remote is somewhere other than GitHub —
    a release cannot be made there."""
    url = gitops.remote_url(project_path) if gitops.is_repo(project_path) \
        else None
    if not url:
        return None
    found = github.parse_remote(url)
    if not found:
        raise PublishError("not-github")
    return found


def released_on_github(token, project_path):
    """Versions already released on GitHub (empty when there is no repo)."""
    try:
        where = target(project_path)
    except PublishError:
        return []
    if not where or not token:
        return []
    found = []
    for name in github.release_tags(token, *where):
        parsed = from_tag(name)
        if parsed:
            found.append(parsed)
    return found


# --------------------------------------------------------------------------- #
# The whole thing
# --------------------------------------------------------------------------- #
def publish(project_path, token, version_text, branch="main", number_hint="",
            repo_name="", private=True, notes="", attach=True,
            release=True, on_status=lambda stage, detail="": None):
    """Push the project to GitHub and, unless `release` is False, publish
    it as a new release.

    Returns {"url", "tag", "title", "assets", "kept_back", "repo"}.
    Stages reported through `on_status`: account, repo, push, release,
    upload (with the file name).
    """
    if not token:
        raise PublishError("signed-out")
    number = channel = pre = ""
    if release:
        number, channel, pre = parse(version_text, number_hint)
    if not gitops.BRANCH.match(branch or ""):
        raise PublishError("bad-branch")

    on_status("account")
    github.validate_token(token)

    if not gitops.is_repo(project_path):
        gitops.init(project_path)
    where = target(project_path)
    if where is None:
        on_status("repo")
        name = repo_name.strip() or github.repo_slug(
            project_path.rstrip("/").rsplit("/", 1)[-1])
        if not github.REPO_NAME.match(name):
            raise PublishError("bad-repo-name")
        created = github.create_repo(token, name, private)
        where = (created["owner"]["login"], created["name"])
        gitops.set_remote(project_path,
                          f"https://github.com/{where[0]}/{where[1]}.git")
    owner, repo = where
    result = {"repo": f"https://github.com/{owner}/{repo}",
              "url": f"https://github.com/{owner}/{repo}/tree/{branch}",
              "tag": "", "title": "", "assets": 0, "kept_back": []}

    the_tag = the_title = ""
    if release:
        the_tag = tag(number, channel, pre)
        the_title = title(number, channel, pre)
        if github.release_by_tag(token, owner, repo, the_tag):
            raise PublishError("exists")

    on_status("push")
    message = f"Release {the_title}" if release else "Petacore sync"
    try:
        commit, kept = gitops.push_branch(project_path, token, branch,
                                          message)
    except gitops.GitError as e:
        if str(e) in ("diverged", "no-remote", "bad-branch"):
            raise PublishError(str(e))
        raise
    result["kept_back"] = kept
    if not release:
        return result

    on_status("release")
    made = github.create_release(token, owner, repo, the_tag, the_title,
                                 commit, channel != "stable", notes)
    result.update(url=made.get("html_url", result["repo"]),
                  tag=the_tag, title=the_title)

    if attach:
        for path in packages_for(project_path, number, channel, pre):
            on_status("upload", path.rsplit("/", 1)[-1])
            github.upload_asset(token, made, path)
            result["assets"] += 1
    return result
