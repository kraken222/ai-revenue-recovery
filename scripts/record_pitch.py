"""Record the pitch video's visual track by driving the real app in a browser.

NOT part of the product. Nothing in `app/` imports this, it is absent from
requirements.txt on purpose, and CI never runs it -- the project's "runs offline with
zero external services" claim would stop being true if a browser engine were a
dependency. Install it only when you need to cut the video:

    pip install playwright imageio-ffmpeg
    python -m playwright install chromium

Then, with the server already running (`uvicorn app.main:app --reload`):

    python -m scripts.record_pitch                 # full 4:45 take
    python -m scripts.record_pitch --speed 12      # ~25s rehearsal, same shots

Output is a silent 1920x1080 webm in `pitch_build/`. Narration is recorded separately
and muxed on afterwards -- see `scripts/build_pitch.py`.

### What is on screen is real

Every frame is either the live app or the actual stdout of a command in this repo. The
terminal shots are genuine captured output, typeset into a page so a browser can film
them; they are not mock-ups, and the capture happens in this script rather than being
pasted in, so they cannot drift from what the commands actually print.

The one thing this cannot film is the GitHub-hosted README and ARCHITECTURE pages if
the machine is offline. Those shots fall back to a local render, and the script says so
rather than silently producing a blank frame.
"""

from __future__ import annotations

import argparse
import html
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BUILD = ROOT / "pitch_build"
BASE = "http://localhost:8000"
REPO = "https://github.com/kraken222/ai-revenue-recovery"

WIDTH, HEIGHT = 1920, 1080

# How long each shot is held, in seconds.
#
# These are NOT the nominal timings in PITCH_NARRATION.md, and the difference is the
# point. A first take at the nominal budgets left three sections with less room than
# their words need at a natural pace -- "what breaks" was 5.3s short -- because page
# loads and settle waits do not divide evenly across sections. The fix is a rebalance,
# not an extension: there was 19.3s of surplus in the roomy sections against 8.8s of
# deficit in the tight ones, and the take was already 4:50 against a five-minute
# ceiling, so buying room by making the video longer was not available.
#
# Re-derive these by running the take, then reading pitch_build/cues.json: room for a
# section is the gap to the next cue, and it must exceed words / 145 * 60.
TIMELINE = {
    "problem": 34,       # 35 -> 31 overshot; 34 leaves ~1.8s spare
    "india": 43,         # was 40, was 0.5s short
    "architecture": 49,  # 50 -> 46 overshot; 49 leaves ~1.6s spare
    "ai_placement": 43,  # 40 left only 0.4s spare
    "measurement": 69,   # was 65, was 3.0s short
    "failures": 41,      # was 35, was 5.3s short - the worst of them
    "close": 19,         # was 20, had 4.4s spare
}

def apply_audio_timeline() -> str | None:
    """Override the shot lengths with ones fitted to real narration audio.

    Written by `build_pitch fit` after measuring where the breaks fall in a finished
    voice track. Cutting the picture to the audio is the only way to guarantee sync
    from a single generation: the alternative needs each section's spoken length known
    before it exists, and a voice engine's pace is its own.
    """
    path = BUILD / "audio_timeline.json"
    if not path.exists():
        return None
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    TIMELINE.update(data["timeline"])
    return (
        f"timed to {data['audio']} "
        f"({data['audio_duration']:.1f}s) via {path.name}"
    )


TERMINAL_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; background: #14140f; color: #e8e6dc;
  font: 20px/1.55 "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace;
  padding: 56px 64px;
}
h1 {
  font-size: 20px; font-weight: 700; letter-spacing: .14em; text-transform: uppercase;
  color: #9fb295; margin: 0 0 28px;
}
pre { margin: 0; white-space: pre; }
.prompt { color: #7fa06f; }
b { color: #ffffff; font-weight: 700; }
.hi { color: #d8c07a; }
"""


def terminal_page(title: str, command: str, output: str) -> str:
    """Typeset real captured stdout as a terminal frame."""
    body = html.escape(output)
    # Draw the eye to the lines that carry the argument, without altering them.
    for needle in (
        "ACTION-CHANGING ERRORS               0/32  (0%)",
        "CI includes zero - effect not established at this n",
        "seeds improved  5/12",
    ):
        body = body.replace(html.escape(needle), f"<b>{html.escape(needle)}</b>")
    return (
        f"<!doctype html><meta charset='utf-8'><style>{TERMINAL_CSS}</style>"
        f"<h1>{html.escape(title)}</h1>"
        f"<pre><span class='prompt'>$</span> {html.escape(command)}\n\n{body}</pre>"
    )


TITLE_CSS = """
:root { color-scheme: light; }
body {
  margin: 0; height: 100vh; display: flex; flex-direction: column;
  justify-content: center; padding: 0 140px; background: #F6F5EE; color: #1B1C18;
  font: 16px/1.5 "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace;
}
h1 { font-size: 76px; letter-spacing: -.035em; margin: 0 0 18px; line-height: 1.02; }
p  { font-size: 26px; color: #5B5E52; margin: 0 0 44px; max-width: 1100px; }
.rule { height: 2px; background: #9FB295; width: 190px; margin-bottom: 44px; }
.meta { font-size: 20px; color: #5F6257; letter-spacing: .04em; }
.meta b { color: #1B1C18; font-weight: 700; }
"""


def title_card(heading: str, sub: str, meta: str) -> str:
    """A held opening frame.

    Not decoration: the first take opened on a blank white page while GitHub loaded,
    which is a dead first second on a video judged in five minutes. A card also covers
    the network latency of the shot that follows it.
    """
    return (
        f"<!doctype html><meta charset='utf-8'><style>{TITLE_CSS}</style>"
        f"<div class='rule'></div><h1>{html.escape(heading)}</h1>"
        f"<p>{html.escape(sub)}</p><div class='meta'>{meta}</div>"
    )


def diagram_page(mermaid_src: Path) -> str | None:
    """Render ARCHITECTURE.md's decision funnel full-bleed, offline.

    The first take filmed this from GitHub and caught a loading spinner: GitHub renders
    mermaid lazily in the client, and the shot held before it finished. Rendering it
    here removes both the timing risk and the network dependency, and the diagram gets
    the whole frame instead of the ~40% column a file view leaves for it.
    """
    if not mermaid_src.exists():
        return None
    text = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    start = text.find("```mermaid")
    if start == -1:
        return None
    end = text.find("```", start + 10)
    graph = text[start + len("```mermaid"): end].strip()

    # The funnel is authored top-down, which is right for a document read in a narrow
    # column and wrong for a 16:9 frame -- rendered TD it becomes a thin vertical strip
    # with tiny type and most of the screen empty. Laying it out left-to-right for the
    # film changes only the direction arrows point, not a single label or edge.
    if graph.startswith("flowchart TD"):
        graph = graph.replace("flowchart TD", "flowchart LR", 1)

    # Rendered explicitly rather than via startOnLoad: the inline script runs after the
    # bundle has parsed, by which point DOMContentLoaded has usually already fired, so
    # startOnLoad silently does nothing and the page shows raw mermaid source. That is
    # exactly what the first attempt filmed.
    return f"""<!doctype html><meta charset='utf-8'>
<style>
  body {{ margin:0; height:100vh; background:#F6F5EE; display:flex; align-items:center;
         justify-content:center; overflow:hidden; }}
  #d {{ width: 86vw; }}
  #d svg {{ width: 100%; height: auto; max-height: 90vh; }}
  #err {{ font: 16px monospace; color: #B3271E; padding: 40px; white-space: pre-wrap; }}
</style>
<div id="d" class="mermaid">{html.escape(graph)}</div>
<script src="{mermaid_src.as_uri()}"></script>
<script>
  (async () => {{
    try {{
      mermaid.initialize({{
        startOnLoad: false,
        theme: 'neutral',
        flowchart: {{ htmlLabels: true, useMaxWidth: true }},
        themeVariables: {{ fontSize: '20px', fontFamily: 'ui-monospace, monospace' }}
      }});
      await mermaid.run({{ querySelector: '#d' }});
    }} catch (e) {{
      // Surface the failure instead of leaving raw source on screen looking deliberate.
      document.body.innerHTML =
        '<div id="err">mermaid failed to render\\n\\n' + (e && e.message) + '</div>';
    }}
  }})();
</script>"""


def capture(command: list[str]) -> str:
    """Run a command in this repo and keep exactly what it printed."""
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=900
    )
    return (result.stdout or "") + (result.stderr or "")


def build_terminal_frames(py: str) -> dict[str, Path]:
    """Capture the two terminal shots as real output, once, before recording."""
    BUILD.mkdir(exist_ok=True)
    frames = {}

    print("  capturing eval_classifier output...")
    text = capture([py, "-m", "scripts.eval_classifier"])
    # The rule-tier "rows the table does NOT define" block is the informative half.
    marker = "Tier 1 - rule table, rows the table does NOT define"
    if marker in text:
        text = text[text.index(marker):]
    path = BUILD / "shot_eval.html"
    path.write_text(
        terminal_page("classifier evaluation", "python -m scripts.eval_classifier", text),
        encoding="utf-8",
    )
    frames["eval"] = path

    ablation = ROOT / "ablation.txt"
    if ablation.exists():
        print("  reusing ablation.txt")
        text = ablation.read_text(encoding="utf-8")
    else:
        print("  running the ablation (this takes a few minutes)...")
        text = capture([py, "-m", "scripts.ablation", "400", "12"])
        ablation.write_text(text, encoding="utf-8")
    path = BUILD / "shot_ablation.html"
    path.write_text(
        terminal_page("ablation - does the learned machinery earn its keep?",
                      "python -m scripts.ablation 400 12", text),
        encoding="utf-8",
    )
    frames["ablation"] = path

    card = BUILD / "shot_title.html"
    card.write_text(
        title_card(
            "AI Revenue Recovery",
            "An agent that finds revenue slipping away and wins it back "
            "— inside what Indian payment rules actually permit.",
            "Razorpay AI Buildathon &middot; <b>Track 03</b> &middot; "
            "github.com/kraken222/ai-revenue-recovery",
        ),
        encoding="utf-8",
    )
    frames["title"] = card

    mermaid_dist = BUILD / "node_modules" / "mermaid" / "dist" / "mermaid.min.js"
    page = diagram_page(mermaid_dist)
    if page:
        path = BUILD / "shot_diagram.html"
        path.write_text(page, encoding="utf-8")
        frames["diagram"] = path
        print("  rendering the decision funnel locally")
    else:
        print("  ! mermaid not installed in pitch_build - the diagram shot will")
        print("    fall back to GitHub, which renders it lazily and may film a spinner.")
        print("    fix:  cd pitch_build && npm install mermaid@11")

    return frames


class Recorder:
    """Wraps a Playwright page with timing that stays honest about the budget."""

    def __init__(self, page, speed: float):
        self.page = page
        self.speed = speed
        self.spent = 0.0
        # Playwright records in real time from context creation, so wall-clock elapsed
        # since then IS the timestamp in the finished file.
        self.t0 = time.monotonic()
        self.cues: list[dict] = []

    def mark(self, section: str) -> None:
        """Record where a section actually begins in the video.

        The scripted budget and the real cut point are not the same thing: page loads
        and settle waits push each shot later than the timeline says. Narration placed
        at nominal timestamps would drift against the picture by a few seconds by the
        end. Emitting the true offsets lets the mux place each clip exactly where its
        shot starts, so a section can never be spoken over the previous shot.
        """
        at = time.monotonic() - self.t0
        self.cues.append({"section": section, "at": round(at, 2)})
        print(f"  [{int(at // 60)}:{int(at % 60):02d}] {section}")

    def hold(self, seconds: float) -> None:
        self.spent += seconds
        self.page.wait_for_timeout(seconds / self.speed * 1000)

    def goto(self, url: str, settle: float = 1.5) -> bool:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception as exc:
            print(f"  ! could not load {url}: {type(exc).__name__}")
            return False
        self.hold(settle)
        return True

    def glide(self, to_y: int, seconds: float, steps: int = 40) -> None:
        """Slow scroll. Jump-cuts read as sloppy on a dense page; a glide lets a judge
        follow where the argument moved."""
        self.page.evaluate(
            """([y, ms]) => new Promise(done => {
                const start = window.scrollY, delta = y - start, t0 = performance.now();
                (function step(now) {
                    const p = Math.min(1, (now - t0) / ms);
                    const ease = p < .5 ? 2*p*p : 1 - Math.pow(-2*p + 2, 2)/2;
                    window.scrollTo(0, start + delta * ease);
                    if (p < 1) requestAnimationFrame(step); else done();
                })(performance.now());
            })""",
            [to_y, seconds / self.speed * 1000],
        )
        self.spent += seconds

    def wait_rendered(self, selector: str, budget: float = 8.0) -> bool:
        """Hold until the element exists, rather than assuming it does.

        The first take filmed a mermaid loading spinner because the shot held on a
        fixed timer. Time spent waiting is charged to the section budget, so a slow
        render eats its own shot instead of pushing everything after it out of sync
        with the narration.
        """
        started = time.monotonic()
        try:
            self.page.wait_for_selector(selector, timeout=budget * 1000, state="attached")
            waited = time.monotonic() - started
            self.spent += waited * self.speed
            return True
        except Exception:
            print(f"  ! {selector} never rendered within {budget}s")
            self.spent += budget
            return False

    def find(self, text: str) -> int | None:
        """Y offset of the first element containing `text`, or None."""
        return self.page.evaluate(
            """(needle) => {
                const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                while (walk.nextNode()) {
                    const el = walk.currentNode;
                    if (el.children.length === 0 && el.textContent.includes(needle)) {
                        return Math.max(0, el.getBoundingClientRect().top + window.scrollY - 120);
                    }
                }
                return null;
            }""",
            text,
        )


def record(speed: float, online: bool) -> Path:
    from playwright.sync_api import sync_playwright

    frames = build_terminal_frames(sys.executable)
    BUILD.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            record_video_dir=str(BUILD / "raw"),
            record_video_size={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=1,
        )
        page = context.new_page()
        r = Recorder(page, speed)

        # --- 0:00 the problem -------------------------------------------------
        r.mark("problem")
        r.goto(frames["title"].as_uri(), settle=0.5)
        r.hold(6)

        readme_ok = r.goto(REPO, settle=3) if online else False
        if not readme_ok:
            print("  ! GitHub unreachable - falling back to the local dashboard")
            r.goto(BASE, settle=2)
        r.hold(TIMELINE["problem"] - 15)
        r.glide(700, 5)

        # --- 0:35 why India breaks it ----------------------------------------
        r.mark("india")
        if readme_ok:
            y = r.find("Why the obvious design is wrong in India")
            if y:
                r.glide(y, 4)
            r.hold(TIMELINE["india"] - 14)
            r.glide((y or 700) + 900, 10)
        else:
            r.hold(TIMELINE["india"])

        # --- 1:15 architecture, then the live chain ---------------------------
        r.mark("architecture")
        arch_seconds = 22
        if "diagram" in frames:
            r.goto(frames["diagram"].as_uri(), settle=0.5)
            r.wait_rendered("#d svg", budget=10)
            r.hold(arch_seconds - 2)
        elif online and r.goto(f"{REPO}/blob/main/ARCHITECTURE.md", settle=4):
            # GitHub renders mermaid lazily; wait for the SVG rather than a timer.
            r.wait_rendered("svg[id^='mermaid']", budget=10)
            y = r.find("what is LEGALLY allowed") or r.find("The one invariant")
            if y:
                r.glide(y, 4)
            r.hold(max(2, arch_seconds - 14))
        else:
            r.hold(arch_seconds)

        r.goto(f"{BASE}/console", settle=2)
        r.hold(TIMELINE["architecture"] - arch_seconds - 2)

        # --- 2:05 where the AI is, and isn't ----------------------------------
        r.mark("ai_placement")
        r.goto(frames["eval"].as_uri(), settle=2)
        r.hold(TIMELINE["ai_placement"] - 2)

        # --- 2:45 measurement, and the flat result ----------------------------
        r.mark("measurement")
        r.goto(BASE, settle=2)
        r.hold(14)
        y = r.find("COMPLIANCE ASSERTIONS") or 200
        r.glide(y, 4)
        r.hold(6)

        r.goto(frames["ablation"].as_uri(), settle=2)
        r.hold(TIMELINE["measurement"] - 30)

        # --- 3:50 what breaks -------------------------------------------------
        r.mark("failures")
        r.goto(f"{BASE}/console", settle=2)
        y = r.find("AWAITING A PERSON") or 0
        if y:
            r.glide(y, 3)
        r.hold(TIMELINE["failures"] - 6)

        # --- 4:25 close -------------------------------------------------------
        r.mark("close")
        if online:
            r.goto(REPO, settle=2)
        else:
            r.goto(BASE, settle=2)
        r.hold(TIMELINE["close"] - 2)

        total = time.monotonic() - r.t0
        print(f"\n  scripted {int(r.spent // 60)}:{int(r.spent % 60):02d}"
              f"   actual {int(total // 60)}:{int(total % 60):02d}")
        context.close()
        browser.close()

    # The cue sheet is what makes the voice line up. Written after close() so `total`
    # reflects the whole take, and consumed by scripts/build_pitch.py.
    import json

    (BUILD / "cues.json").write_text(
        json.dumps({"duration": round(total, 2), "cues": r.cues}, indent=2),
        encoding="utf-8",
    )
    print(f"  cue sheet: {BUILD / 'cues.json'}")

    raw = sorted((BUILD / "raw").glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not raw:
        raise SystemExit("playwright produced no video file")
    out = BUILD / "pitch_visual.webm"
    out.unlink(missing_ok=True)
    raw[-1].rename(out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speed", type=float, default=1.0,
                        help="time compression for rehearsal; 12 gives a ~25s preview")
    parser.add_argument("--offline", action="store_true",
                        help="skip the GitHub shots and film only the local app")
    args = parser.parse_args()

    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen(f"{BASE}/health", timeout=5)
    except (urllib.error.URLError, OSError):
        print(f"The app is not running at {BASE}.")
        print("Start it first:  uvicorn app.main:app --reload")
        return 1

    fitted = apply_audio_timeline()
    print(f"Recording at {WIDTH}x{HEIGHT}, speed x{args.speed}")
    if fitted:
        print(f"  {fitted}")
    else:
        print("  using the default shot lengths (no audio_timeline.json)")
    out = record(args.speed, online=not args.offline)
    size = out.stat().st_size / 1e6
    print(f"\nwrote {out}  ({size:.1f} MB)")
    if args.speed != 1.0:
        print("This was a rehearsal pass - re-run without --speed for the real take.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
