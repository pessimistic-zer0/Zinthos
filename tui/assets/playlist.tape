# Zinthos — mood playlist demo (vibe → smoothly-sequenced set)
# Render from the REPO ROOT:
#   nix develop .#tui -c vhs tui/assets/playlist.tape
# Same prerequisites as demo.tape (engine up + release build).

Output docs/playlist.gif

Set Shell "bash"
Set FontSize 22
Set Width 1400
Set Height 820
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 75ms
Set CursorBlink false

# ── launch (hidden) ──
Hide
Type "tui/target/release/sonic-tui"
Enter
Sleep 3s
Show

# ── menu → Mood Playlist (tile 3) ──
Sleep 2s
Type "3"
Sleep 800ms

# ── describe the vibe ──
Type "energetic upbeat happy dance pop"
Sleep 600ms
Enter
Sleep 3500ms

# ── scroll the transition-ordered set ──
Down
Sleep 600ms
Down
Sleep 600ms
Down
Sleep 600ms
Down
Sleep 600ms
Down
Sleep 1500ms

# ── inspect one track ──
Enter
Sleep 2500ms

Hide
Type "q"
Sleep 300ms
Ctrl+C
