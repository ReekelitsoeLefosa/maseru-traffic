"""Generate the app icons (Windows, Android, iOS) from one drawing.

    python tools/make_icons.py
Writes frontend/icons/*.png, desktop/app.ico, store/ (Google Play listing graphics) and, once the
Android project exists, its launcher icons.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
BLUE, GREEN, WHITE = (0, 32, 159), (0, 149, 67), (255, 255, 255)


def draw(size: int, maskable: bool = False) -> Image.Image:
    s = 4  # draw large, then downscale for smooth edges
    W = size * s
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:   # Android crops maskable icons to a circle/squircle: fill everything, keep art in the middle 80%
        d.rectangle([0, 0, W, W], fill=BLUE)
        pad = int(W * 0.18)
    else:
        d.rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.22), fill=BLUE)
        pad = int(W * 0.12)
    # green band at the bottom (Lesotho flag)
    band = int(W * 0.12)
    d.rectangle([0, W - band - (0 if maskable else int(W * 0.0)), W, W], fill=GREEN)
    if not maskable:   # keep rounded corners on the band
        mask = Image.new("L", (W, W), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.22), fill=255)
        img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (W, W), 0), mask))
        d = ImageDraw.Draw(img)
    # traffic light housing
    hw = (W - 2 * pad) * 0.42
    cx = W / 2
    top, bottom = pad, W - pad - band * 0.6
    d.rounded_rectangle([cx - hw / 2, top, cx + hw / 2, bottom], radius=hw * 0.28, fill=(20, 24, 33), outline=WHITE,
                        width=max(2, int(W * 0.012)))
    r = hw * 0.30
    h = bottom - top
    for i, col in enumerate([(230, 57, 53), (245, 183, 0), (46, 204, 64)]):
        cy = top + h * (0.2 + 0.3 * i)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    return img.resize((size, size), Image.LANCZOS)


def traffic_light(size: int) -> Image.Image:
    """Transparent foreground layer for Android adaptive icons (art inside the central safe zone)."""
    s = 4
    W = size * s
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    hw, h = W * 0.22, W * 0.50
    cx, top = W / 2, (W - h) / 2
    d.rounded_rectangle([cx - hw / 2, top, cx + hw / 2, top + h], radius=hw * 0.28, fill=(20, 24, 33),
                        outline=WHITE, width=max(2, int(W * 0.012)))
    r = hw * 0.30
    for i, col in enumerate([(230, 57, 53), (245, 183, 0), (46, 204, 64)]):
        cy = top + h * (0.2 + 0.3 * i)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    return img.resize((size, size), Image.LANCZOS)


def store_assets():
    """Google Play listing: 512x512 icon and the 1024x500 feature graphic."""
    from PIL import ImageFont
    out = ROOT / "store"
    out.mkdir(exist_ok=True)
    draw(512, maskable=True).convert("RGB").save(out / "play-icon-512.png")
    fg = Image.new("RGB", (1024, 500), BLUE)
    d = ImageDraw.Draw(fg)
    d.rectangle([0, 440, 1024, 500], fill=GREEN)
    light = traffic_light(440)
    fg.paste(light, (-20, 10), light)
    x, max_w = 360, 1024 - 360 - 50

    def font(name, size):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            return ImageFont.load_default()

    size = 64
    while size > 30 and d.textlength("Maseru Smart Traffic", font=font("segoeuib.ttf", size)) > max_w:
        size -= 2
    d.text((x, 140), "Maseru Smart Traffic", font=font("segoeuib.ttf", size), fill=WHITE)
    small = font("segoeui.ttf", 32)
    d.text((x, 150 + size + 20), "Live junction traffic · faster routes", font=small, fill=WHITE)
    d.text((x, 150 + size + 65), "Alerts when your trip gets busy", font=small, fill=WHITE)
    fg.save(out / "feature-graphic-1024x500.png")
    print(f"Store graphics written to {out}")


def android_icons():
    """Launcher icons for the Capacitor Android project (run after `npx cap add android`)."""
    res = ROOT / "mobile" / "android" / "app" / "src" / "main" / "res"
    if not res.exists():
        print("No Android project yet (run `npx cap add android` in mobile/); skipping launcher icons")
        return
    for dpi, legacy, fore in (("mdpi", 48, 108), ("hdpi", 72, 162), ("xhdpi", 96, 216),
                              ("xxhdpi", 144, 324), ("xxxhdpi", 192, 432)):
        folder = res / f"mipmap-{dpi}"
        folder.mkdir(exist_ok=True)
        draw(legacy).save(folder / "ic_launcher.png")
        circle = Image.new("L", (legacy * 4, legacy * 4), 0)
        ImageDraw.Draw(circle).ellipse([0, 0, legacy * 4 - 1, legacy * 4 - 1], fill=255)
        rnd = draw(legacy, maskable=True)
        rnd.putalpha(circle.resize((legacy, legacy), Image.LANCZOS))
        rnd.save(folder / "ic_launcher_round.png")
        traffic_light(fore).save(folder / "ic_launcher_foreground.png")
    # notification icon: white silhouette on transparent (Android tints it)
    for dpi, px in (("mdpi", 24), ("hdpi", 36), ("xhdpi", 48), ("xxhdpi", 72), ("xxxhdpi", 96)):
        folder = res / f"drawable-{dpi}"
        folder.mkdir(exist_ok=True)
        s = px * 4
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        hw, h = s * 0.42, s * 0.9
        cx, top = s / 2, s * 0.05
        d.rounded_rectangle([cx - hw / 2, top, cx + hw / 2, top + h], radius=hw * 0.3, fill=WHITE)
        r = hw * 0.28
        for i in range(3):   # punch the three lamps out as holes
            cy = top + h * (0.2 + 0.3 * i)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0, 0))
        im.resize((px, px), Image.LANCZOS).save(folder / "ic_stat_traffic.png")
    bg = res / "values" / "ic_launcher_background.xml"
    bg.parent.mkdir(exist_ok=True)
    bg.write_text('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
                  '    <color name="ic_launcher_background">#00209F</color>\n'
                  '    <color name="notification_color">#00209F</color>\n</resources>\n', encoding="utf-8")
    print(f"Android launcher icons written to {res}")


def main():
    out = ROOT / "frontend" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        draw(size).save(out / f"icon-{size}.png")
        draw(size, maskable=True).save(out / f"icon-maskable-{size}.png")
    # iOS home-screen icon must be opaque (iOS adds its own rounded corners)
    ios = Image.new("RGB", (180, 180), BLUE)
    ios.paste(draw(180, maskable=True), (0, 0))
    ios.save(out / "apple-touch-icon.png")
    draw(32).save(out / "favicon-32.png")
    ico = ROOT / "desktop" / "app.ico"
    ico.parent.mkdir(exist_ok=True)
    draw(256).save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Icons written to {out} and {ico}")
    store_assets()
    android_icons()


if __name__ == "__main__":
    main()
