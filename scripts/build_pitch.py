"""Split the narration for TTS, and mux the finished voice onto the recorded visuals.

NOT part of the product -- same standing as `record_pitch.py`. Needs `imageio-ffmpeg`
(a bundled ffmpeg binary, so no system install).

Two steps, because the middle one happens outside this repo.

    python -m scripts.build_pitch split
        Writes pitch_build/narration/NN_section.txt -- one file per section of
        PITCH_NARRATION.md, in order, with the target duration of each. Feed these to a
        text-to-speech engine and save the returned audio beside them as NN_section.mp3.

    python -m scripts.build_pitch mux
        Concatenates the narration audio in order and lays it over
        pitch_build/pitch_visual.webm, producing pitch_build/pitch.mp4.

### Why the split is per section rather than one long file

The visual track is cut to the section budgets in PITCH_NARRATION.md. One continuous
audio file drifts against those cuts -- a few seconds of accumulated difference and the
ablation numbers are on screen while the voice is still on compliance. Per-section
audio lets `mux` report the drift for each section instead of hiding it in a total, and
pad the short ones so every section starts where the picture expects it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BUILD = ROOT / "pitch_build"
NARR = BUILD / "narration"
SCRIPT = ROOT / "PITCH_NARRATION.md"
VISUAL = BUILD / "pitch_visual.webm"
OUT = BUILD / "pitch.mp4"

SECTION_RE = re.compile(
    r"^## (\d):(\d\d) – (\d):(\d\d) · ([^\n*]+?)\s*\*\((\d+) words\)\*\n(.*?)(?=^## |\Z)",
    re.M | re.S,
)


def ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def parse_sections() -> list[dict]:
    text = SCRIPT.read_text(encoding="utf-8")
    sections = []
    for m in SECTION_RE.finditer(text):
        start = int(m.group(1)) * 60 + int(m.group(2))
        end = int(m.group(3)) * 60 + int(m.group(4))
        body = m.group(7)
        # Strip markdown emphasis: a TTS engine reads asterisks aloud as "asterisk" or
        # stumbles on them. The emphasis is a cue for a human reader, not for the voice.
        body = re.sub(r"\*\*(.+?)\*\*", r"\1", body, flags=re.S)
        body = re.sub(r"\*(.+?)\*", r"\1", body, flags=re.S)
        body = re.sub(r"[ \t]*\n[ \t]*", " ", body)
        body = re.sub(r"\s{2,}", " ", body).strip()
        sections.append(
            {
                "slug": re.sub(r"[^a-z0-9]+", "_", m.group(5).lower()).strip("_"),
                "title": m.group(5).strip(),
                "start": start,
                "budget": end - start,
                "words": int(m.group(6)),
                "text": body,
            }
        )
    return sections


def cmd_split() -> int:
    sections = parse_sections()
    if not sections:
        print(f"no sections parsed from {SCRIPT.name} - has its heading format changed?")
        return 1

    NARR.mkdir(parents=True, exist_ok=True)
    print(f"{len(sections)} sections -> {NARR}\n")
    total = 0
    for i, s in enumerate(sections, 1):
        path = NARR / f"{i:02d}_{s['slug']}.txt"
        path.write_text(s["text"], encoding="utf-8")
        total += s["budget"]
        print(f"  {path.name:<34}{s['words']:>4}w  budget {s['budget']:>3}s")

    print(f"\n  total budget {total // 60}:{total % 60:02d}")
    print(
        "\nGenerate speech for each .txt and save it beside the text as the same\n"
        "basename with a .mp3 extension. Keep the numbering: `mux` orders by filename.\n"
        "\nUse one voice for all of them, and keep the pace even -- a section read fast\n"
        "and a section read slow will not line up with a picture cut to fixed budgets."
    )
    return 0


def duration(path: Path) -> float:
    out = subprocess.run(
        [ffmpeg(), "-i", str(path)], capture_output=True, text=True
    ).stderr
    m = re.search(r"Duration: (\d+):(\d\d):(\d\d\.\d+)", out)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def cmd_mux() -> int:
    if not VISUAL.exists():
        print(f"no visual track at {VISUAL}")
        print("record it first:  python -m scripts.record_pitch")
        return 1

    sections = parse_sections()
    clips = sorted(NARR.glob("*.mp3")) + sorted(NARR.glob("*.wav"))
    clips = sorted(clips, key=lambda p: p.name)
    if not clips:
        print(f"no narration audio in {NARR}")
        print("run `python -m scripts.build_pitch split`, generate speech, save as .mp3")
        return 1
    if len(clips) != len(sections):
        print(f"! {len(clips)} audio file(s) for {len(sections)} sections.")
        print("  Every section needs one, or the voice will run against the wrong picture.")
        return 1

    print("section timing, voice against picture:\n")
    drift = 0.0
    for s, clip in zip(sections, clips):
        spoken = duration(clip)
        delta = spoken - s["budget"]
        drift += delta
        flag = "" if abs(delta) <= 3 else ("  LONG" if delta > 0 else "  SHORT")
        print(
            f"  {s['title'][:34]:<36}budget {s['budget']:>3}s  spoken {spoken:>6.1f}s"
            f"  {delta:+5.1f}s{flag}"
        )
    print(f"\n  cumulative drift {drift:+.1f}s")
    if abs(drift) > 8:
        print(
            "  ! More than eight seconds adrift. Re-read the long sections rather than\n"
            "    letting the mux stretch them - the picture is cut to these budgets."
        )

    concat = BUILD / "narration_concat.txt"
    concat.write_text(
        "\n".join(f"file '{c.resolve().as_posix()}'" for c in clips), encoding="utf-8"
    )
    voice = BUILD / "narration.m4a"
    subprocess.run(
        [ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(concat), "-c:a", "aac", "-b:a", "192k", str(voice)],
        check=True,
    )

    # -shortest so a voice track longer than the picture cannot leave the video running
    # on a frozen final frame, which reads as a mistake rather than a hold.
    subprocess.run(
        [ffmpeg(), "-y", "-loglevel", "error",
         "-i", str(VISUAL), "-i", str(voice),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-shortest", str(OUT)],
        check=True,
    )

    size = OUT.stat().st_size / 1e6
    print(f"\nwrote {OUT}  ({size:.1f} MB, {duration(OUT):.0f}s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=["split", "mux"])
    args = parser.parse_args()
    return cmd_split() if args.step == "split" else cmd_mux()


if __name__ == "__main__":
    raise SystemExit(main())
