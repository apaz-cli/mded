# mded

Edit markdown while looking at the rendered page. Each block renders through
pandoc with your site's stylesheet. Click a block to edit its markdown, then
press Esc or click elsewhere and it renders again. Blocks you don't edit stay
byte-for-byte as they were. A `::: {.cls}` fenced div (nested ones too) or a
raw `<div>` with markdown inside is edited as a single block.

    ./blog                      # ~/git/clean-site/blog, builds with generate.py
    ./mded.py DIR --style FILE --build "CMD {file}" [--root DIR] [--port 8123]

Keys: Ctrl+S save · Ctrl+B save and build · Ctrl+Shift+V view built html ·
Ctrl+E whole-file source · Ctrl+Enter new block · ↑/↓ move across blocks ·
Ctrl+Z / Ctrl+Shift+Z undo/redo (outside a block) · Ctrl+click follows links.

Live sync: the open file is watched, and changes made on disk (vim, git,
scripts) show up right away. Untouched blocks keep their rendering, and your
unsaved edits are merged block by block. A block changed both here and on disk
is shown both ways until you pick one. Autosave (toolbar toggle, on by default)
saves shortly after each finished block edit.

Needs python3 and pandoc. Serves on 127.0.0.1 only.
