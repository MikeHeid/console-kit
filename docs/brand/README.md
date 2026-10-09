# Overture brand assets

The mark is an open ring (the **O**, and an overture's opening), a dial
needle, and a green dot in the ring's gap: one operator's control plane,
cueing what ships. Colours are the console's own tokens from
`plugin/kit/overture/console.css`: accent `#58a6ff` / `#0969da`, built
`#3fb950` / `#1a7f37`, on `#0d1117`. Text is Inter SemiBold, outlined to
paths, so the SVGs render the same without the font installed.

| File | Use |
| --- | --- |
| `overture-icon.svg`, `overture-icon-{512,192,180,64,32,16}.png` | App icon on a rounded dark tile (180 = Apple touch icon, 192/512 = web manifest) |
| `favicon.ico` | 16, 32 and 64 px in one file |
| `overture-mark-dark.svg`, `overture-mark-light.svg` | The mark alone, transparent, for dark or light backgrounds |
| `overture-logo-dark.svg` / `.png` | Mark + wordmark for **dark** backgrounds (light text) |
| `overture-logo-light.svg` / `.png` | Mark + wordmark for **light** backgrounds (dark text) |
| `overture-social.svg` / `.png` | 1280×640 project card: GitHub social preview, link previews, slides |

**GitHub social preview:** Settings → General → Social preview → upload
`overture-social.png`.

**A README header that follows the reader's theme:**

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/overture-logo-dark.svg">
  <img alt="Overture" src="docs/brand/overture-logo-light.svg" height="72">
</picture>
```

Keep clear space around the mark of at least the green dot's diameter, and
don't recolour the dot: green is the console's "built" state.
