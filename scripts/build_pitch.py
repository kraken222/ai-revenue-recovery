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


# Spoken forms for tokens a speech engine reliably mangles. Applied only to the text
# handed to TTS -- PITCH_NARRATION.md keeps the written form, which is what a human
# reader wants on a teleprompter. Deliberately short: every entry here is a token that
# was actually wrong when read aloud, not a guess about what might be.
SPOKEN = [
    (r"\b5xx\b", "five-hundred-class"),
    (r"\b409\b", "four-oh-nine"),
    (r"\bHMAC\b", "H-MAC"),
    (r"\bAFA\b", "A-F-A"),
    (r"\beNACH\b", "e-NACH"),
]


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
        for pattern, spoken in SPOKEN:
            body = re.sub(pattern, spoken, body)
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

    cues = load_cues()
    sheet = write_voiceover_sheet(sections, cues)
    print(f"\nwrote {sheet}")
    print(
        "\nGenerate speech for each .txt and save it beside the text as the same\n"
        "basename with a .mp3 extension. Keep the numbering: `mux` orders by filename."
    )
    if not cues:
        print(
            "\n! No cues.json yet, so the sheet carries nominal times. Record first\n"
            "  (python -m scripts.record_pitch) for measured ones."
        )
    return 0


def write_voiceover_sheet(sections: list[dict], cues: dict | None) -> Path:
    """One page carrying everything needed to cut the voice: the text, the hard limit
    on each section, and where it lands in the video."""
    starts = [c["at"] for c in cues["cues"]] if cues else [s["start"] for s in sections]
    total = cues["duration"] if cues else sum(s["budget"] for s in sections)

    def clock(t: float) -> str:
        return f"{int(t // 60)}:{t % 60:04.1f}"

    lines = [
        "# Voiceover sheet",
        "",
        f"Video: `pitch_build/pitch_visual.webm` — {clock(total)} at 1920x1080.",
        "",
        "**Each clip is placed at an absolute timestamp, not joined end to end.** So a",
        "section that runs a little long or short only affects itself — it cannot shift",
        "everything after it. What you must not do is exceed the *room* column, because",
        "that audio would still be playing after the picture has cut to the next shot.",
        "",
        "Settings that matter more than the voice you pick:",
        "",
        "- **One voice for all seven.** Switching mid-video reads as an error.",
        "- **Same stability/similarity settings for all seven**, or the pace drifts",
        "  between sections and the later ones stop fitting their room.",
        "- Leave no leading or trailing silence — placement already handles the gaps.",
        "",
        "| # | Section | Starts at | Room | Words | File |",
        "|---|---|---|---|---|---|",
    ]
    for i, (s, start) in enumerate(zip(sections, starts), 1):
        room = (starts[i] - start) if i < len(starts) else total - start
        lines.append(
            f"| {i} | {s['title']} | `{clock(start)}` | **{room:.1f}s** | {s['words']} | "
            f"`{i:02d}_{s['slug']}.mp3` |"
        )

    lines += ["", "---", ""]
    for i, (s, start) in enumerate(zip(sections, starts), 1):
        room = (starts[i] - start) if i < len(starts) else total - start
        lines += [
            f"## {i:02d} · {s['title']}",
            "",
            f"Starts `{clock(start)}` · must not exceed **{room:.1f}s** · "
            f"{s['words']} words · save as `{i:02d}_{s['slug']}.mp3`",
            "",
            "```",
            s["text"],
            "```",
            "",
        ]

    lines += [
        "---",
        "",
        "## When all seven exist",
        "",
        "```bash",
        "python -m scripts.build_pitch mux",
        "```",
        "",
        "It prints, per section, where the clip lands and whether it overruns its shot,",
        "then writes `pitch_build/pitch.mp4`. If a section overruns it says so rather",
        "than quietly stretching anything.",
    ]

    path = BUILD / "VOICEOVER.md"
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


def duration(path: Path) -> float:
    out = subprocess.run(
        [ffmpeg(), "-i", str(path)], capture_output=True, text=True
    ).stderr
    m = re.search(r"Duration: (\d+):(\d\d):(\d\d\.\d+)", out)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def load_cues() -> dict | None:
    """Where each section actually starts in the recorded file.

    Written by scripts/record_pitch.py. Without it the only available reference is the
    nominal timeline, and the two are not the same: page loads and settle waits push
    each shot a little later than scripted, so by the close the difference is several
    seconds.
    """
    path = BUILD / "cues.json"
    if not path.exists():
        return None
    import json

    return json.loads(path.read_text(encoding="utf-8"))


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

    # Each clip is PLACED at the timestamp its shot begins, not concatenated onto the
    # end of the previous one. Concatenation makes every section's start depend on how
    # long every earlier section ran, so one slow read shifts everything after it and
    # the close ends up spoken over the wrong picture. Placing at absolute offsets means
    # a section that runs long or short only affects itself.
    cues = load_cues()
    if cues:
        starts = [c["at"] for c in cues["cues"]]
        source = "measured from the recording"
    else:
        starts = [s["start"] for s in sections]
        source = "nominal timeline - no cues.json, so re-record for exact placement"
    if len(starts) != len(sections):
        print(f"! cue sheet has {len(starts)} marks for {len(sections)} sections")
        return 1

    print(f"section placement ({source}):\n")
    worst = 0.0
    for i, (s, clip, start) in enumerate(zip(sections, clips, starts)):
        spoken = duration(clip)
        # How much room this shot actually has, from its start to the next one's.
        room = (starts[i + 1] - start) if i + 1 < len(starts) else (cues or {}).get(
            "duration", start + s["budget"]
        ) - start
        over = spoken - room
        worst = max(worst, over)
        flag = "" if over <= 0.5 else "  OVERRUNS THE NEXT SHOT"
        print(
            f"  {s['title'][:32]:<34}starts {start:>6.1f}s  room {room:>5.1f}s"
            f"  spoken {spoken:>5.1f}s  {over:+5.1f}s{flag}"
        )

    if worst > 0.5:
        print(
            f"\n  ! {worst:.1f}s of overrun. That audio will still be playing when the\n"
            "    picture cuts, so the voice describes the previous shot. Re-generate the\n"
            "    offending section shorter, or widen its budget in record_pitch.py's\n"
            "    TIMELINE and re-record. The mux will not silently paper over it."
        )
    else:
        print("\n  every section fits inside its own shot")

    voice = BUILD / "narration.m4a"
    # adelay pads each clip to its absolute start; amix sums them onto one track.
    inputs, filters, labels = [], [], []
    for i, (clip, start) in enumerate(zip(clips, starts)):
        inputs += ["-i", str(clip)]
        ms = int(round(start * 1000))
        filters.append(f"[{i}:a]adelay={ms}|{ms},apad=pad_dur=0[a{i}]")
        labels.append(f"[a{i}]")
    graph = (
        ";".join(filters)
        + ";"
        + "".join(labels)
        + f"amix=inputs={len(clips)}:duration=longest:normalize=0[out]"
    )
    subprocess.run(
        [ffmpeg(), "-y", "-loglevel", "error", *inputs,
         "-filter_complex", graph, "-map", "[out]",
         "-c:a", "aac", "-b:a", "192k", str(voice)],
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
