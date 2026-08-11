PETACORE — DOSYA TÜRÜ LOGOLARI
==============================

Bu klasöre koyduğun SVG/PNG logolar, Proje sayfasındaki dosya listesinde
uzantıya göre otomatik gösterilir (HTML'deki <img src="..."> mantığı).

BOYUT: SVG öner (kare tuval, örn. 128x128). PNG kullanacaksan 64x64.
İSİMLENDİRME: aşağıdaki isimler BİREBİR böyle olmalı (küçük harf).
Hem .svg hem .png kabul edilir; ikisi de varsa .svg kazanır.

DOSYA ADI          UZANTILAR
python.svg         .py  .pyw
javascript.svg     .js  .mjs  .cjs
typescript.svg     .ts  .tsx
html.svg           .html  .htm
css.svg            .css  .scss  .sass
c.svg              .c  .h
cpp.svg            .cpp  .hpp  .cc  .hh  .cxx
csharp.svg         .cs
java.svg           .java
rust.svg           .rs
go.svg             .go
php.svg            .php
ruby.svg           .rb
swift.svg          .swift
kotlin.svg         .kt  .kts
lua.svg            .lua
shell.svg          .sh  .bash  .zsh
sql.svg            .sql
json.svg           .json
markdown.svg       .md  .markdown

İKİ KONUMDAN BİRİNE KOYABİLİRSİN:
1) ~/.local/share/petacore/filetype-icons/
   → ANINDA etkili olur, yeniden kurulum gerekmez (önerilen).
2) Bu klasör (kaynak koddaki petacore/filetype-icons/)
   → install.sh ile birlikte kurulur, uygulamayla dağıtılır.

Not: Dil logolarının çoğu tescilli markadır; kendi çizimlerini yapman
(Inkscape planın) dağıtım için en temiz yoldur.
