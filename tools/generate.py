#!/usr/bin/env python3
"""
Dial'ın demo playlist'i: Play ve App Store incelemesinin girişi, mağaza görsellerinin kaynağı. Bütün içerik telifsiz ve gerçek (Wikimedia
Commons'tan kamu malı, CC0, CC BY, CC BY-SA; NASA'nın müziksiz kaydı). Liste `tools/sources.json`'da; her öğenin lisansı ve yazarı orada,
lisans sayfası (`index.html`) ondan üretiliyor.

Nerede ne duruyor:
- GitHub Pages (bu repo, main kökü): `dial.m3u` (adresi değişmez: Play'in inceleme talimatı bu linki veriyor), afişler `posters/`,
  sahne görselleri `backdrops/`, logolar `logos/`, lisans sayfası `index.html`.
- GitHub Releases: videolar. `media` sürümünde filmler (`<film>.mp4`, 720p) ve bölümler (`<dizi>-e<n>.mp4`, 480p); `live-<kanal>`
  sürümlerinde kanalların döngüsünün HLS parçaları (480p, 6 sn).
- Cloudflare Worker (`worker/`, ücretsiz plan, `demo`/`demo`): Xtream ucu (diziler yalnız orada), kanalların saate göre kayan HLS
  listesi (Releases'taki parçalara mutlak adresle) ve yayın akışı (günün tarihiyle, süresi dolmuyor). `dial.m3u` canlı kanalları ve
  `url-tvg`'yi de Worker'dan alıyor. Kod `worker/template.js`, veriyle birleşmiş hali `worker/worker.js` (Cloudflare'in düzenleyicisine
  yapıştırılıp Deploy).
- `build/` (git'e girmez): indirilenler ve kodlananlar.

Adımlar:  python3 tools/generate.py [adım …] [--only id,id]
  download  kaynakları indir (Commons'un hazır küçük kopyası, yoksa orijinal; NASA'nın mp4'ü)
  encode    filmler, bölümler, kanallar
  art       afişler, sahne görselleri, logolar (Chrome'un başsız moduyla)
  site      dial.m3u ve lisans sayfası (index.html)
  upload    videoları GitHub Releases'a yükle (`gh`; adımlara dahil değil)
  worker    Cloudflare Worker'ın kodu `worker/worker.js` (Xtream ucu, canlı HLS, yayın akışı; adresi DIAL_WORKER)
  sheets    afişin karesini seçmek için aday kareler (`build/sheets/`; adımlara dahil değil)
  (adım verilmezse hepsi)
ffmpeg: ~/.local/bin/ffmpeg (FFMPEG ortam değişkeniyle değişir)
"""

import argparse
import concurrent.futures
import html
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
BUILD = ROOT / "build"
SOURCE = BUILD / "source"
OUT = BUILD / "out"
FONTS = TOOLS / "fonts"
FFMPEG = os.environ.get("FFMPEG", str(pathlib.Path.home() / ".local/bin/ffmpeg"))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_AGENT = "DialDemoBuilder/1.0 (demo playlist generator)"

SITE = "https://nejatboy.github.io/Dial-Demo"
RELEASES = "https://github.com/nejatboy/Dial-Demo/releases/download"
WORKER = os.environ.get("DIAL_WORKER", "https://dial-demo.njsoft.workers.dev")

SEGMENT_SECONDS = 6

# Grupların rengi (logolar ve afişlerin etiketi); uygulamanın koyu zemininde okunan, birbirinden ayrılan tonlar.
GROUP_COLORS = {
    "Space": "#3B5BDB",
    "Nature": "#2F9E44",
    "Ocean": "#1098AD",
    "Animation": "#E8590C",
    "Sci-Fi": "#7048E8",
    "Travel": "#C2255C",
    "Weather": "#5C7CFA",
}

LICENSE_URLS = {
    "CC0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC BY 2.0": "https://creativecommons.org/licenses/by/2.0/",
    "CC BY 2.5": "https://creativecommons.org/licenses/by/2.5/",
    "CC BY 3.0": "https://creativecommons.org/licenses/by/3.0/",
    "CC BY 4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC BY-SA 3.0": "https://creativecommons.org/licenses/by-sa/3.0/",
    "CC BY-SA 4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
    "CC BY-SA 3.0 igo": "https://creativecommons.org/licenses/by-sa/3.0/igo/",
}


def run(command, **kwargs):
    subprocess.run(command, check=True, **kwargs)


def fetch_json(url):
    """Commons sık isteği 429'la geri çeviriyor: istekler arası 2 sn, 429'da artan bekleme."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(6):
        time.sleep(2)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 5:
                raise
            time.sleep(30 * (attempt + 1))


def load_sources():
    return json.loads((TOOLS / "sources.json").read_text(encoding="utf-8"))


def length(source):
    """Kodlanan videonun süresi: kaynağın başından `start` saniye kırpılıyor (NASA'nın logolu tanıtım kartı)."""
    return source["duration"] - source.get("start", 0)


def source_key(source):
    """İndirilen dosyanın adı: kaynağın kimliği, dosya adına uygun."""
    name = source.get("commons") or source.get("nasa")
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:120]


def all_sources(data, only=None):
    """Her öğenin kaynağı ve hangi çözünürlükte gerektiği (genişlik); kanal klipleri 480p."""
    wanted = {}

    def add(source, width):
        key = source_key(source)
        wanted[key] = (source, max(width, wanted.get(key, (None, 0))[1]))

    movies = {movie["id"]: movie for movie in data["movies"]}

    for movie in data["movies"]:
        if not only or movie["id"] in only:
            add(movie["source"], 1280)

    for series in data["series"]:
        if not only or series["id"] in only:
            for episode in series["episodes"]:
                add(episode["source"], 854)

    for channel in data["channels"]:
        if not only or channel["id"] in only:
            for clip in channel["clips"]:
                add(movies[clip["movie"]]["source"] if "movie" in clip else clip, 854)

    return wanted


# ---------------------------------------------------------------- indirme


def commons_url(source, width):
    """Commons'un hazır kopyalarından istenen genişliği karşılayan en küçüğü; yoksa orijinal."""
    title = "File:" + source["commons"]
    query = urllib.parse.urlencode({"action": "query", "format": "json", "titles": title, "prop": "videoinfo", "viprop": "derivatives|url"})
    page = next(iter(fetch_json(f"https://commons.wikimedia.org/w/api.php?{query}")["query"]["pages"].values()))
    info = page["videoinfo"][0]
    candidates = [
        derivative for derivative in info.get("derivatives", [])
        if derivative.get("transcodekey", "").endswith(".webm") and int(derivative.get("width", 0)) >= width
    ]

    if candidates:
        return min(candidates, key=lambda derivative: int(derivative["width"]))["src"]

    return info["url"].split("?")[0]


def nasa_url(source):
    collection = fetch_json(f"https://images-assets.nasa.gov/video/{urllib.parse.quote(source['nasa'])}/collection.json")
    files = [url for url in collection if url.endswith(".mp4")]

    for suffix in ("~large.mp4", "~medium.mp4", "~orig.mp4"):
        for url in files:
            if url.endswith(suffix):
                return secure(url)

    return secure(files[0])


def secure(url):
    """NASA'nın adresleri http ve yolda boşluklu geliyor."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(("https", parts.netloc, urllib.parse.quote(parts.path), "", ""))


def download(data, only):
    """Hazır kopya inmezse orijinal deneniyor; inmeyenler sonda listeleniyor, betik durmuyor (yeniden çalıştırınca yalnız onlar iner)."""
    SOURCE.mkdir(parents=True, exist_ok=True)
    failed = []

    for key, (source, width) in all_sources(data, only).items():
        if list(SOURCE.glob(key + ".*")):
            continue

        try:
            urls = [nasa_url(source)] if "nasa" in source else [commons_url(source, width), commons_url(source, 10 ** 6)]
        except Exception as error:
            failed.append(f"{key}: {error}")
            continue

        for url in dict.fromkeys(urls):
            extension = pathlib.Path(urllib.parse.urlparse(url).path).suffix or ".webm"
            target = SOURCE / (key + extension)
            print(f"indir {key} ← {url[:100]}", flush=True)
            result = subprocess.run(["curl", "-fsSL", "--retry", "5", "--retry-delay", "10", "--retry-all-errors", "-A", USER_AGENT,
                                     "-o", str(target) + ".part", url])
            if result.returncode == 0:
                os.replace(str(target) + ".part", target)
                break
        else:
            failed.append(key)

    if failed:
        raise SystemExit("inmeyenler (yeniden çalıştır):\n  " + "\n  ".join(failed))


def source_file(source):
    files = list(SOURCE.glob(source_key(source) + ".*"))
    if not files:
        raise SystemExit(f"önce indir: {source_key(source)}")

    return files[0]


# ---------------------------------------------------------------- kodlama


def has_audio(path):
    probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    return "Audio:" in probe


def encode_file(source_path, target, height, crf, maxrate, start=0):
    """Film ve bölüm: tek MP4, H.264 ve AAC, başı hızlı (faststart); oran korunuyor, yalnız küçültülüyor."""
    if target.exists():
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    video = f"scale=w=-2:h='min({height},ih)':flags=lanczos,format=yuv420p"
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-i", str(source_path), "-map", "0:v:0", "-map", "0:a:0?",
               "-vf", video, "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", str(crf), "-maxrate", maxrate,
               "-bufsize", "8M", "-g", "48", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", str(target) + ".part.mp4"]
    print(f"kodla {target.name}", flush=True)
    run(command)
    os.replace(str(target) + ".part.mp4", target)


def encode_channel(channel, movies):
    """Kanal: klipler 854×480'e (kenar payıyla) ve 25 kareye eşitlenip art arda, 6 sn'lik HLS parçalarına bölünüyor."""
    folder = OUT / f"live-{channel['id']}"
    playlist = folder / "loop.m3u8"
    if playlist.exists():
        return

    folder.mkdir(parents=True, exist_ok=True)
    inputs, filters, labels = [], [], []

    for index, clip in enumerate(channel["clips"]):
        source = movies[clip["movie"]]["source"] if "movie" in clip else clip
        path = source_file(source)
        start = clip.get("start", 0)
        length = clip.get("length")
        inputs += ["-ss", str(start)] + (["-t", str(length)] if length else []) + ["-i", str(path)]
        filters.append(f"[{index}:v:0]scale=854:480:force_original_aspect_ratio=decrease:flags=lanczos,pad=854:480:(ow-iw)/2:(oh-ih)/2,"
                       f"setsar=1,fps=25,format=yuv420p[v{index}]")

        if has_audio(path):
            filters.append(f"[{index}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{index}]")
        else:
            duration = length or source["duration"]
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration}[a{index}]")

        labels.append(f"[v{index}][a{index}]")

    filters.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=1[v][a]")
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
               "-c:v", "libx264", "-profile:v", "main", "-preset", "medium", "-crf", "23", "-maxrate", "1500k", "-bufsize", "3M",
               "-g", "50", "-keyint_min", "50", "-sc_threshold", "0", "-c:a", "aac", "-b:a", "96k",
               "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_playlist_type", "vod",
               "-hls_segment_filename", str(folder / f"{channel['id']}-%03d.ts"), str(playlist)]
    print(f"kanal {channel['id']}", flush=True)
    run(command)


def encode(data, only):
    movies = {movie["id"]: movie for movie in data["movies"]}
    jobs = []

    for movie in data["movies"]:
        if not only or movie["id"] in only:
            jobs.append((encode_file, source_file(movie["source"]), OUT / "media" / f"{movie['id']}.mp4", 720, 22, "3000k", movie["source"].get("start", 0)))

    for series in data["series"]:
        if not only or series["id"] in only:
            for number, episode in enumerate(series["episodes"], start=1):
                jobs.append((encode_file, source_file(episode["source"]), OUT / "media" / f"{series['id']}-e{number}.mp4", 480, 23, "1500k",
                             episode["source"].get("start", 0)))

    for channel in data["channels"]:
        if not only or channel["id"] in only:
            jobs.append((encode_channel, channel, movies))

    # libx264 zaten çok çekirdekli; ikişer iş makineyi dolduruyor.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(job[0], *job[1:]) for job in jobs]:
            future.result()


# ---------------------------------------------------------------- görseller


def frame(video, seconds, target):
    """Videonun bir karesi (afiş ve sahne görselinin zemini)."""
    if target.exists():
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(seconds), "-i", str(video), "-frames:v", "1", "-q:v", "2", str(target)])


def render(html_text, width, height, target):
    """HTML'i Chrome'un başsız moduyla görüntüye çeviriyor (fontlar `tools/fonts/`)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".html", dir=BUILD, delete=False, encoding="utf-8") as page:
        page.write(html_text)

    png = target.with_suffix(".png") if target.suffix != ".png" else target
    run([CHROME, "--headless=new", "--hide-scrollbars", "--force-device-scale-factor=1", f"--window-size={width},{height}",
         "--allow-file-access-from-files", f"--screenshot={png}", f"file://{page.name}"], stderr=subprocess.DEVNULL)
    os.unlink(page.name)

    if target.suffix == ".jpg":
        run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85", str(png), "--out", str(target)], stdout=subprocess.DEVNULL)
        png.unlink()


FONT_FACE = f"""
@font-face {{ font-family: Outfit; src: url('file://{FONTS}/outfit.ttf'); font-weight: 100 900; }}
@font-face {{ font-family: DMSans; src: url('file://{FONTS}/dm_sans.ttf'); font-weight: 100 900; }}
"""


def poster_html(image, title, group):
    color = GROUP_COLORS.get(group, "#495057")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{FONT_FACE}
html, body {{ margin: 0; width: 600px; height: 900px; overflow: hidden; background: #0B0B0D; }}
.art {{ position: absolute; inset: 0; background: url('file://{image}') center / cover no-repeat; }}
.shade {{ position: absolute; inset: 0; background: linear-gradient(180deg, rgba(11,11,13,0) 45%, rgba(11,11,13,.92) 82%, #0B0B0D 100%); }}
.text {{ position: absolute; left: 40px; right: 40px; bottom: 48px; }}
.tag {{ display: inline-block; padding: 6px 14px; border-radius: 999px; background: {color}; color: #fff;
       font: 600 20px/1 DMSans; letter-spacing: .5px; text-transform: uppercase; }}
h1 {{ margin: 18px 0 0; color: #F2F2F4; font: 700 64px/1.02 Outfit; letter-spacing: -1px; }}
</style></head><body><div class="art"></div><div class="shade"></div>
<div class="text"><span class="tag">{html.escape(group)}</span><h1>{html.escape(title)}</h1></div></body></html>"""


def logo_html(name, group, initials=None):
    words = [word for word in re.split(r"[\s-]+", name) if word]
    initials = initials or ("".join(word[0] for word in words[:2]).upper() if len(words) > 1 else name[:2].upper())
    color = GROUP_COLORS.get(group, "#495057")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{FONT_FACE}
html, body {{ margin: 0; width: 256px; height: 256px; overflow: hidden; background: {color}; }}
div {{ display: flex; align-items: center; justify-content: center; width: 256px; height: 256px;
      color: #fff; font: 700 112px/1 Outfit; letter-spacing: -2px; }}
</style></head><body><div>{html.escape(initials)}</div></body></html>"""


def art(data, only):
    frames = BUILD / "frames"

    def artwork(item_id, title, group, video, seconds, duration):
        """Afiş ve sahne görseli aynı kareden: `poster_at` (sn; dizide [bölüm, sn]) seçilmişse o, yoksa videonun %30'u."""
        still = frames / f"{item_id}.jpg"
        frame(video, seconds if seconds is not None else round(duration * 0.3, 1), still)
        render(poster_html(still, title, group), 600, 900, ROOT / "posters" / f"{item_id}.jpg")
        backdrop = ROOT / "backdrops" / f"{item_id}.jpg"
        if not backdrop.exists():
            backdrop.parent.mkdir(parents=True, exist_ok=True)
            run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(still), "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
                 "-q:v", "3", str(backdrop)])

    for movie in data["movies"]:
        if not only or movie["id"] in only:
            seconds = movie.get("poster_at")
            seconds = None if seconds is None else seconds - movie["source"].get("start", 0)
            artwork(movie["id"], movie["title"], movie["group"], OUT / "media" / f"{movie['id']}.mp4", seconds, length(movie["source"]))

    for series in data["series"]:
        if not only or series["id"] in only:
            number, seconds = series.get("poster_at", [1, None])
            episode = series["episodes"][number - 1]
            seconds = None if seconds is None else seconds - episode["source"].get("start", 0)
            artwork(series["id"], series["title"], series["group"], OUT / "media" / f"{series['id']}-e{number}.mp4", seconds, length(episode["source"]))

            for number, episode in enumerate(series["episodes"], start=1):
                still = ROOT / "backdrops" / f"{series['id']}-e{number}.jpg"
                frame(OUT / "media" / f"{series['id']}-e{number}.mp4", round(length(episode["source"]) * 0.4, 1), still)

    for channel in data["channels"]:
        if not only or channel["id"] in only:
            render(logo_html(channel["name"], channel["group"], channel.get("logo")), 256, 256, ROOT / "logos" / f"{channel['id']}.png")


def sheets(data, only):
    """Afişin karesini seçmek için: her video için 12 aday kare, üstünde saniyesi (`build/sheets/<id>.jpg`)."""
    folder = BUILD / "sheets"
    folder.mkdir(parents=True, exist_ok=True)
    items = [(movie["id"], OUT / "media" / f"{movie['id']}.mp4", length(movie["source"])) for movie in data["movies"]]

    for series in data["series"]:
        for number, episode in enumerate(series["episodes"], start=1):
            items.append((f"{series['id']}-e{number}", OUT / "media" / f"{series['id']}-e{number}.mp4", length(episode["source"])))

    for item_id, video, duration in items:
        if only and not any(item_id.startswith(key) for key in only):
            continue

        times = [round(duration * (index + 0.5) / 12, 1) for index in range(12)]
        inputs, labels = [], []

        for index, seconds in enumerate(times):
            inputs += ["-ss", str(seconds), "-i", str(video)]
            labels.append(f"[{index}:v]trim=end_frame=1,scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2,"
                          f"drawtext=fontfile={FONTS}/dm_sans.ttf:text='{seconds}':x=8:y=8:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.6[f{index}]")

        rows = [f"{''.join(f'[f{row * 4 + column}]' for column in range(4))}hstack=inputs=4[r{row}]" for row in range(3)]
        graph = ";".join(labels + rows + ["[r0][r1][r2]vstack=inputs=3"])
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph, "-frames:v", "1", "-q:v", "3", str(folder / f"{item_id}.jpg")])


# ---------------------------------------------------------------- site


def clip_title(clip, movies):
    if "movie" in clip:
        return movies[clip["movie"]]["title"]
    if clip.get("title"):
        return clip["title"]

    name = re.sub(r"\.(webm|ogv)$", "", clip.get("commons", ""), flags=re.I)
    name = re.sub(r"\s*\((?:[^)]*\d{5,}[^)]*|SVS[^)]*|NESDIS[^)]*|KSC[^)]*)\)", "", name)
    return re.sub(r"\s+", " ", name).strip()


def segments(channel):
    playlist = (OUT / f"live-{channel['id']}" / "loop.m3u8").read_text().splitlines()
    pairs = []

    for index, line in enumerate(playlist):
        if line.startswith("#EXTINF:"):
            pairs.append((float(line[8:].rstrip(",")), playlist[index + 1].strip()))

    return pairs


def site(data):
    """Pages'in dosyaları: `dial.m3u` (canlı kanallar Worker'ın kayan HLS listesinden, yayın akışı Worker'ın XMLTV'sinden, filmler
    Releases'tan) ve lisans sayfası. Diziler M3U'da yok (türü yok), yalnız Xtream'de (Worker)."""
    lines = [f'#EXTM3U url-tvg="{WORKER}/xmltv.php?username=demo&password=demo"']

    for channel in data["channels"]:
        logo = f"{SITE}/logos/{channel['id']}.png"
        lines += [f'#EXTINF:-1 tvg-id="{channel["id"]}" tvg-name="{channel["name"]}" tvg-logo="{logo}" group-title="{channel["group"]}",{channel["name"]}',
                  f"{WORKER}/hls/{channel['id']}.m3u8"]

    for movie in data["movies"]:
        poster = f"{SITE}/posters/{movie['id']}.jpg"
        lines += [f'#EXTINF:-1 tvg-name="{movie["title"]}" tvg-logo="{poster}" group-title="{movie["group"]}",{movie["title"]}',
                  f"{RELEASES}/media/{movie['id']}.mp4"]

    (ROOT / "dial.m3u").write_text("\n".join(lines) + "\n")
    (ROOT / "index.html").write_text(license_page(data), encoding="utf-8")
    print("site: dial.m3u, index.html")


def credit(title, source):
    if "nasa" in source:
        link = f"https://images.nasa.gov/details/{urllib.parse.quote(source['nasa'])}"
        origin = "NASA Image and Video Library"
    else:
        link = "https://commons.wikimedia.org/wiki/File:" + urllib.parse.quote(source["commons"].replace(" ", "_"))
        origin = "Wikimedia Commons"

    license_name = source["license"]
    license_link = LICENSE_URLS.get(license_name)
    license_html = f'<a href="{license_link}">{html.escape(license_name)}</a>' if license_link else html.escape(license_name)
    return (f'<li><b>{html.escape(title)}</b> · {html.escape(source["author"])} · {license_html} · '
            f'<a href="{link}">{origin}</a></li>')


def license_page(data):
    movies = {movie["id"]: movie for movie in data["movies"]}
    sections = ["<h2>Movies</h2><ul>" + "".join(credit(movie["title"], movie["source"]) for movie in data["movies"]) + "</ul>"]
    sections.append("<h2>Series</h2><ul>" + "".join(
        credit(f"{series['title']} · {episode['title']}", episode["source"]) for series in data["series"] for episode in series["episodes"]) + "</ul>")
    channel_items = []

    for channel in data["channels"]:
        for clip in channel["clips"]:
            if "movie" not in clip:
                channel_items.append(credit(f"{channel['name']} · {clip_title(clip, movies)}", clip))

    sections.append("<h2>Live channels</h2><p>Channels also loop the movies above.</p><ul>" + "".join(channel_items) + "</ul>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dial demo playlist</title><style>
body {{ margin: 0; background: #0B0B0D; color: #A1A1AA; font: 15px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
main {{ max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }}
h1, h2, b {{ color: #F2F2F4; }} a {{ color: #F2F2F4; }} li {{ margin: 6px 0; }}
</style></head><body><main>
<h1>Dial demo playlist</h1>
<p>A playlist for trying Dial, the media player for your own playlists. Dial doesn't provide any content; everything here is free to share:
public domain works (including works of the U.S. government such as NASA, NOAA, NPS and USGS), CC0, CC BY and CC BY-SA. Videos are re-encoded
(and channel clips joined into loops); titles shown in the app are ours, the original titles are on each source page.</p>
<p>M3U: <a href="{SITE}/dial.m3u">{SITE}/dial.m3u</a></p>
{"".join(sections)}
</main></body></html>
"""


def origin_line(source):
    """Kaynağın kısa atfı (programın açıklaması, filmin özeti): yazar ve lisans; ayrıntı lisans sayfasında."""
    where = "NASA" if "nasa" in source else "Wikimedia Commons"
    return f"{source['author']} · {source['license']} · {where}"


def worker(data):
    """Worker'ın kodu: `worker/template.js` ve içine gömülen veri (kategoriler, kanalların parçaları, programlar, filmler, diziler)."""
    movies = {movie["id"]: movie for movie in data["movies"]}
    groups = {kind: list(dict.fromkeys(item["group"] for item in data[key])) for kind, key in (("live", "channels"), ("movies", "movies"), ("series", "series"))}
    categories = {kind: [{"id": base + index + 1, "name": name} for index, name in enumerate(names)]
                  for (kind, names), base in zip(groups.items(), (0, 100, 200))}
    category_id = {kind: {item["name"]: item["id"] for item in items} for kind, items in categories.items()}
    live = []

    for number, channel in enumerate(data["channels"], start=1):
        programs = []
        for clip in channel["clips"]:
            source = movies[clip["movie"]]["source"] if "movie" in clip else clip
            programs.append({"title": clip_title(clip, movies), "description": origin_line(source)})

        live.append({"num": number, "id": number, "key": channel["id"], "name": channel["name"], "category": category_id["live"][channel["group"]],
                     "logo": f"{SITE}/logos/{channel['id']}.png", "segments": [round(duration, 3) for duration, _ in segments(channel)],
                     "programs": programs})

    movie_items = [{"num": number, "id": 1000 + number, "key": movie["id"], "name": movie["title"], "category": category_id["movies"][movie["group"]],
                    "genre": movie["group"], "poster": f"{SITE}/posters/{movie['id']}.jpg", "backdrop": f"{SITE}/backdrops/{movie['id']}.jpg",
                    "seconds": length(movie["source"]), "plot": origin_line(movie["source"]), "url": f"{RELEASES}/media/{movie['id']}.mp4"}
                   for number, movie in enumerate(data["movies"], start=1)]
    series_items = []

    for number, series in enumerate(data["series"], start=1):
        authors = list(dict.fromkeys(episode["source"]["author"] for episode in series["episodes"]))
        series_items.append({
            "num": number, "id": 2000 + number, "key": series["id"], "name": series["title"], "category": category_id["series"][series["group"]],
            "genre": series["group"], "cover": f"{SITE}/posters/{series['id']}.jpg", "backdrop": f"{SITE}/backdrops/{series['id']}.jpg",
            "plot": " · ".join(authors) + ". Sources and licenses: " + SITE + "/",
            "episodes": [{"id": 10000 + number * 100 + episode_number, "number": episode_number, "title": episode["title"],
                          "seconds": length(episode["source"]), "still": f"{SITE}/backdrops/{series['id']}-e{episode_number}.jpg",
                          "url": f"{RELEASES}/media/{series['id']}-e{episode_number}.mp4"}
                         for episode_number, episode in enumerate(series["episodes"], start=1)],
        })

    payload = {"site": SITE, "releases": RELEASES, "added": "1798761600", "categories": categories, "live": live, "movies": movie_items,
               "series": series_items}
    template = (ROOT / "worker" / "template.js").read_text(encoding="utf-8")
    (ROOT / "worker" / "worker.js").write_text(template.replace("__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))), encoding="utf-8")
    print(f"worker/worker.js: {len(live)} kanal, {len(movie_items)} film, {len(series_items)} dizi")


def upload(data, only):
    """Videolar GitHub Releases'a (`gh`): `media` (filmler, bölümler) ve kanal başına `live-<kanal>`; var olan dosya değiştiriliyor."""
    repository = "nejatboy/Dial-Demo"
    notes = f"Video files of the Dial demo playlist. Sources and licenses: {SITE}/"
    releases = {"media": sorted((OUT / "media").glob("*.mp4"))}

    for channel in data["channels"]:
        releases[f"live-{channel['id']}"] = sorted((OUT / f"live-{channel['id']}").glob("*.ts"))

    existing = subprocess.run(["gh", "release", "list", "-R", repository, "--limit", "100", "--json", "tagName", "-q", ".[].tagName"],
                              capture_output=True, text=True, check=True).stdout.split()

    for tag, files in releases.items():
        if only and tag not in only and tag.removeprefix("live-") not in only:
            continue

        if tag not in existing:
            run(["gh", "release", "create", tag, "-R", repository, "--title", tag, "--notes", notes, "--latest=false"])

        print(f"yükle {tag}: {len(files)} dosya", flush=True)

        for start in range(0, len(files), 20):
            run(["gh", "release", "upload", tag, "-R", repository, "--clobber", *map(str, files[start:start + 20])])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("steps", nargs="*", default=["download", "encode", "art", "site", "worker"])
    parser.add_argument("--only", default="")
    arguments = parser.parse_args()
    only = set(filter(None, arguments.only.split(",")))
    data = load_sources()
    BUILD.mkdir(exist_ok=True)

    for step in arguments.steps:
        {"download": lambda: download(data, only), "encode": lambda: encode(data, only), "art": lambda: art(data, only),
         "site": lambda: site(data), "sheets": lambda: sheets(data, only),
         "upload": lambda: upload(data, only), "worker": lambda: worker(data)}[step]()


if __name__ == "__main__":
    main()
