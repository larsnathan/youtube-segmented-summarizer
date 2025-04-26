#!/usr/bin/env python3
from nicegui import ui
from pathlib import Path
import yt_dlp, asyncio, functools, logging, uuid
from database import init_db, downloads, ENGINE
from sqlalchemy.orm import Session
from sqlalchemy import select
import textwrap

TMP_DIR = Path("/filestore")  # video + captions
CAPT_FMT = "vtt"
CAPT_LANG = "en"
REFRESH_SEC = 10  # how often the table updates

logging.basicConfig(level=logging.INFO)
init_db()


# ────────────────────────── yt-dlp helper ───────────────────────────────────
def yt_download(url: str, file_id: str) -> tuple[str, str, str]:
    out_template = str(TMP_DIR / f"{file_id}.%(ext)s")
    with yt_dlp.YoutubeDL({"verbose": True, "cookiefile": "./cookies.txt"}) as probe:
        probe.cookiejar
        info = probe.extract_info(url, download=False)
        title = info.get("title", "unknown-title")

    opts = {
        "outtmpl": out_template,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [CAPT_LANG],
        "subtitlesformat": CAPT_FMT,
        "cookiefile": "./cookies.txt",
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    video = f"{TMP_DIR}/{file_id}.mp4"
    caption = f"{TMP_DIR}/{file_id}.{CAPT_LANG}.{CAPT_FMT}"
    return title, video, caption


# ─────────────────────── summaries UI helper ───────────────────────────────
def fetch_summaries():
    with Session(ENGINE) as ses:
        rows = ses.execute(
            select(downloads.c.uuid, downloads.c.title, downloads.c.summary).where(
                downloads.c.status == "summarized"
            )
        ).all()
    result = []
    for uuid_, title, summary_path in rows:
        try:
            first_line = Path(summary_path).read_text(encoding="utf-8").splitlines()[0]
            first_line = textwrap.shorten(first_line, width=80, placeholder="…")
        except Exception:
            first_line = "[cannot read summary]"
        result.append(
            {"uuid": uuid_, "title": title, "snippet": first_line, "path": summary_path}
        )
    return result


def build_summary_table(container):
    container.clear()
    table = ui.table(
        columns=[
            {"name": "title", "label": "Title", "field": "title", "sortable": True},
            {"name": "uuid", "label": "UUID", "field": "uuid"},
            {"name": "snip", "label": "Summary", "field": "snippet"},
        ],
        rows=fetch_summaries(),
        row_key="uuid",
        pagination=10,
    ).classes("w-full")

    # make rows clickable
    def download(row):
        ui.download(row["path"])

    table.on("row-click", download)


# ─────────────────────────── main page ──────────────────────────────────────
@ui.page("/")
def main() -> None:
    ui.label("YouTube Video + Captions Downloader").classes("text-2xl font-bold mb-4")
    url_input = ui.input("YouTube link").classes("w-full")
    progress = ui.linear_progress(value=0, show_value=False).classes(
        "w-full mt-4 hidden"
    )
    output = ui.label().classes("block mt-2")

    async def handle_download():
        url = (url_input.value or "").strip()
        if not url:
            output.text = "❌ Please paste a link first."
            return

        url_input.disable()
        dl_button.disable()
        output.text = ""
        progress.classes(remove="hidden")
        progress.value = 0.2

        file_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        try:
            title, video_path, caption_path = await loop.run_in_executor(
                None, functools.partial(yt_download, url, file_id)
            )
            with Session(ENGINE) as ses:
                ses.execute(
                    downloads.insert().values(
                        uuid=file_id,
                        title=title,
                        video=video_path,
                        caption=caption_path,
                        status="downloaded",
                    )
                )
                ses.commit()
            progress.value = 1.0
            output.text = f"✅ Saved as {file_id}.mp4 / .{CAPT_FMT}"
        except Exception as e:
            output.text = f"⚠️ {e}"
            logging.exception(e)
        finally:
            progress.classes(add="hidden")
            url_input.enable()
            dl_button.enable()
            progress.value = 0

    dl_button = ui.button("Download", on_click=handle_download).classes("mt-3")

    # -- summaries section ----------------------------------------------------
    ui.separator().classes("my-6")
    ui.label("Summaries").classes("text-xl font-semibold mb-2")

    summary_table = ui.table(
        columns=[
            {"name": "title", "label": "Title", "field": "title", "sortable": True},
            {"name": "uuid", "label": "UUID", "field": "uuid"},
            {"name": "snip", "label": "Snippet", "field": "snippet"},
        ],
        rows=fetch_summaries(),
        row_key="uuid",
        pagination=10,
    ).classes("w-full")

    # ── pop-up dialog (edge-to-edge overlay) ────────────────────────────────
    dialog = ui.dialog().props("maximized").classes("p-0 m-0")

    with dialog:
        # full-size, margin-free card
        with ui.card().classes("w-full h-full bg-white max-w-none p-0"):

            # header: title + close button
            with ui.row().classes("items-center justify-between w-full p-4"):
                dialog_title = ui.label().classes("text-lg font-semibold")
                ui.button(icon="close", on_click=dialog.close)

            # body: flex-grow so it uses all remaining space, scrolls internally
            dialog_body = (
                ui.column()
                .classes("w-full flex-grow")
                .style("overflow-y:auto")  # remove 60 vh cap
            )

    def build_expansions(full_txt: str):
        dialog_body.clear()

        lines = full_txt.splitlines()
        segments = []
        overall = []

        for line in lines:
            if line.startswith("[") and "]" in line:
                segments.append(line)
            elif line.strip().startswith("== OVERALL SUMMARY"):
                overall = lines[lines.index(line) + 1 :]
                break

        # build every accordion INSIDE the dialog_body container
        with dialog_body:
            # chunk expansions
            for seg in segments:
                ts, text = seg.split("]", 1)
                with ui.expansion(ts.strip("[]"), icon="timer").classes(
                    "border-b w-full"
                ):
                    ui.markdown(text.strip()).classes("w-full")

            # overall summary
            if overall:
                with ui.expansion(
                    "OVERALL SUMMARY", icon="summarize", value=False
                ).classes("border-t border-b w-full"):
                    ui.markdown("\n".join(overall).strip()).classes("w-full")

    def show_full_summary(event):
        row = event.args[1]
        try:
            full = Path(row["path"]).read_text("utf-8")
        except Exception as e:
            full = f"**Error opening file**\n\n```\n{e}\n```"

        dialog_title.text = f"{row['title']}  •  {row['uuid']}"
        build_expansions(full)  # rebuild inside dialog
        dialog.open()

    summary_table.on("row-click", show_full_summary)

    # refresh the rows periodically
    def refresh_rows():
        summary_table.rows = fetch_summaries()
        summary_table.update()

    ui.timer(REFRESH_SEC, refresh_rows)


ui.run(port=10101, reload=False)
