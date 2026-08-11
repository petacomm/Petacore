PETACORE — PAYLAŞIM KARTI ŞABLONLARI
====================================

Kendi tasarımını buraya koy, Petacore verileri onun içine yerleştirsin.

DOSYA ADLARI (boyuta göre):
    story.svg     1080 x 1920   (9:16)
    square.svg    1080 x 1080   (1:1)
    wide.svg      1920 x 1080   (16:9)

Bir dosya varsa Petacore onu kullanır, yoksa yerleşik tasarımı çizer.

ÇOK ÖNEMLİ — METİNLER YAZI OLARAK KALMALI
Inkscape'te: Dosya → Farklı Kaydet → "Düz SVG (Plain SVG)".
Metinleri "Path → Object to Path" ile şekle ÇEVİRME; çevirirsen yazı artık
düzenlenemez ve değerler yerleştirilemez.

YER TUTUCULAR
Tasarımında değerlerin gireceği yere şu metinleri yaz:

    {{PROJECT}}     proje adı
    {{LINES}}       kod satırı        (örn. 10.8K)
    {{CHARS}}       karakter          (örn. 458.8K)
    {{FILES}}       dosya sayısı      (örn. 70)
    {{PLATFORM}}    hedef sistem      (örn. Linux / Windows)

    {{LANG1}} {{PCT1}}    en büyük dil ve yüzdesi
    {{LANG2}} {{PCT2}}    ikinci dil
    {{LANG3}} {{PCT3}}    üçüncü dil
    {{LANG4}} {{PCT4}}    dördüncü dil

Kullanılmayan yer tutucular (örneğin projede tek dil varsa {{LANG2}})
otomatik olarak boşaltılır.

DİL ŞERİDİ
Şeridin bölümlerine id ver; Petacore genişliklerini yüzdelere göre ayarlar:

    <rect id="bar1" .../>   en büyük dil
    <rect id="bar2" .../>   ikinci dil
    <rect id="bar3" .../>   üçüncü
    <rect id="bar4" .../>   dördüncü

Şerit bölümleri, hepsinin toplam genişliği korunacak şekilde yeniden
paylaştırılır; kullanılmayanların genişliği sıfırlanır.

DİL LOGOLARI
İstersen logo yerine bir kare çiz ve id ver:

    <rect id="icon1" .../>   ilk dilin logosu buraya yerleşir
    <rect id="icon2" .../>   ikinci dilin logosu

Petacore, filetype-icons klasöründeki ilgili SVG'yi o kutunun boyutunda
oraya yerleştirir.
