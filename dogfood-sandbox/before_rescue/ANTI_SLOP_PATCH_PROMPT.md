# 🧹 Anti-Slop Surgical De-Slop Prompt

Paste this prompt into **Claude Code**, **Cursor**, or **Windsurf** to eliminate AI Slop:

```markdown
Please de-slop this project by resolving the following 50 deterministic UI & Copy violations.
Follow `DESIGN.md` and `design-tokens.css` strictly (4-step font scale, 3-step mineral surfaces, 1px hairline borders, zero purple radial blobs, zero UI chrome emojis, and clear Korean ASD-STE100 + Toss copy):

1. `DESIGN.md:1` (`missing_design_lock`): Found `Missing Reference-First design lock files` → Run `anti-slop lock . --archetype linear-precision` to generate `DESIGN.md` and `design-tokens.css` before styling components.
2. `index.html:5` (`viewport_zoom_block`): Found `<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-sc` → Remove `user-scalable=no` and `maximum-scale=1.0`; use 16px (`--font-title`) on inputs to prevent iOS auto-zoom.
3. `index.html:226` (`fractional_font_slop`): Found `font-size: 10.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
4. `index.html:263` (`fractional_font_slop`): Found `font-size: 10.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
5. `index.html:381` (`fractional_font_slop`): Found `font-size: 12.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
6. `index.html:464` (`fractional_font_slop`): Found `font-size: 11.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
7. `index.html:511` (`fractional_font_slop`): Found `font-size: 10.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
8. `index.html:524` (`fractional_font_slop`): Found `font-size: 11.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
9. `index.html:644` (`fractional_font_slop`): Found `font-size: 14.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
10. `index.html:767` (`fractional_font_slop`): Found `font-size: 10.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
11. `index.html:817` (`fractional_font_slop`): Found `font-size: 12.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
12. `index.html:915` (`fractional_font_slop`): Found `font-size: 12.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
13. `index.html:981` (`fractional_font_slop`): Found `font-size: 11.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
14. `index.html:1090` (`fractional_font_slop`): Found `font-size: 12.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
15. `index.html:1207` (`fractional_font_slop`): Found `font-size: 11.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
16. `index.html:1347` (`fractional_font_slop`): Found `font-size: 12.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
17. `index.html:1364` (`fractional_font_slop`): Found `font-size: 11.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
18. `index.html:1452` (`fractional_font_slop`): Found `font-size: 14.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
19. `index.html:1504` (`fractional_font_slop`): Found `font-size: 12.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
20. `index.html:1516` (`fractional_font_slop`): Found `font-size: 11.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
21. `index.html:1526` (`fractional_font_slop`): Found `font-size: 11.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
22. `index.html:1548` (`fractional_font_slop`): Found `font-size: 13.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
23. `index.html:1552` (`fractional_font_slop`): Found `font-size: 11.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
24. `index.html:1615` (`fractional_font_slop`): Found `font-size: 10.5px` → Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).
25. `index.html:1684` (`emoji_chrome_slop`): Found `<button class="vm-btn" data-view-mode="agent">🤖` → Replace decorative UI emojis with monochrome 1.5px stroke SVGs or `.status-dot-sm` semantic indicators.
```
