# slack icon generate — make a Slack app icon from any image

A harness's bot personas each get a Slack app, and every app wants an icon.
Slack rejects most raw downloads. This turns whatever the user grabbed
(webp, tiny jpeg, huge ChatGPT png, non-square screenshot) into a compliant icon.

## Slack icon constraints (what upload actually enforces)

| Constraint | Rule |
|---|---|
| Ratio | exactly square (1:1) — "Icons must be squares" |
| Dimensions | between **512×512** and **2000×2000** px |
| File size | under **2MB** (big 1:1 PNGs, e.g. ChatGPT images, routinely exceed this) |
| Format | PNG or JPEG (webp must be converted) |

Target output: **1024×1024** — comfortable headroom on both bounds, and small
sources upscaled to 1024 stay acceptable at icon display sizes.

## 0 — Inspect first, always

```bash
magick identify -format "%wx%h %m %A\n" <file>   # size, format, alpha
ls -lh <file>                                     # file size
```

Then **look at the image** (Read tool). The squaring strategy depends on the
content, not just the numbers. Note anything worth warning about: watermarks
(IGN.com in a corner), very small sources (<300px → visibly soft after upscale).

## 1 — Pick the squaring strategy

Decision tree, in order:

1. **Already square** → no squaring; go to step 2.
2. **Face / portrait** (character close-up) → **center crop** to the short side.
   Padding a portrait looks wrong; faces survive a crop. Verify the face is fully
   inside the crop window before accepting.
   ```bash
   magick in.jpg -gravity center -crop <S>x<S>+0+0 +repage out.png   # S = short side
   ```
3. **Object on a flat background** (white/solid, or image has alpha) → **pad**
   with that background color.
   ```bash
   magick in.webp -background white -alpha remove -alpha off \
     -gravity center -background white -extent <L>x<L> out.png       # L = long side
   ```
4. **Subject fills the frame, or background is a gradient/texture** (flat pad
   would show seams, crop would cut the subject) → **blur-extend**: blow up a
   blurred copy to fill the square, composite the original centered on top.
   ```bash
   magick in.jpg -write mpr:orig \
     -resize <L>x<L>^ -gravity center -extent <L>x<L> -blur 0x14 \
     mpr:orig -gravity center -composite out.png
   ```
   Sample edge pixels if unsure whether the background is flat:
   `magick in.jpg -format "%[pixel:p{2,10}] %[pixel:p{2,H/2}]" info:`

## 2 — Resize into range

```bash
magick square.png -resize 1024x1024 <name>-slack-icon.png
```

(Chain this onto the step-1 command in practice — one magick invocation.)

## 3 — File size gate

`ls -lh` the result. If **over 2MB** (or the source was a >2MB square PNG that
needed no other work), convert to high-quality JPEG instead of shrinking pixels:

```bash
magick big.png -quality 92 <name>-slack-icon.jpg   # ~2.8MB PNG → ~750KB, visually identical
```

## 4 — Verify and report

- Read the output image — confirm the subject is intact, padding blends, nothing
  important got cropped.
- Confirm dimensions and file size one last time
  (`magick identify -format "%wx%h\n"` + `ls -lh`).
- Name outputs `<unit>-slack-icon.png|jpg`, keep the original untouched.
- Report which strategy was used per file and repeat any warnings (soft upscale,
  watermark) so the user can re-source a better image if they care.

## Fallback without ImageMagick

`sips` (macOS built-in) covers the simple cases:

```bash
sips -z 1024 1024 -s format png in.jpeg --out out.png        # square resize + convert
sips -c <S> <S> in.jpeg --out out.jpg                        # center crop
sips -p <L> <L> --padColor FFFFFF in.png --out out.png       # pad (flat color only)
```

No blur-extend equivalent — if the image needs it, install ImageMagick
(`brew install imagemagick`).
