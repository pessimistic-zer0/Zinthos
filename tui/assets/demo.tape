# Zinthos — hero demo (search → results → detail → similar)
# Render from the REPO ROOT so the Output path and the binary path resolve:
#   nix develop .#tui -c vhs tui/assets/demo.tape
# Requires: the engine up on 127.0.0.1:3000, and a release build of the TUI
#   (cargo build --release --manifest-path tui/Cargo.toml).
# See tui/assets/CLAUDE.md for the full recording runbook.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 22
Set Width 1500
Set Height 840
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 75ms
Set CursorBlink false

# ── launch (hidden): start the prebuilt TUI and let it connect to the engine ──
Hide
Type "tui/target/release/sonic-tui"
Enter
Sleep 3s
Show

# ── menu: let the banner breathe ──
Sleep 2500ms

# ── Search ──
Enter
Sleep 800ms
Type "dark moody instrumental electronic"
Sleep 600ms
Enter
Sleep 3s

# ── walk the results ──
Down
Sleep 700ms
Down
Sleep 500ms
Down
Sleep 500ms
Down
Sleep 1s

# ── open a track's detail card ──
Enter
Sleep 2500ms

# ── "more like this" (nearest-neighbour FAISS search) ──
Type "s"
Sleep 3s
Down
Sleep 600ms
Down
Sleep 600ms
Down
Sleep 2s

# ── back out cleanly ──
Type "q"
Sleep 1500ms

Hide
Ctrl+C
