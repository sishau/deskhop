# Generates tools/text-channel/icon.ico (multi-size) with Pillow only.
# Run: python gen_icon.py
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

NAVY = (30, 41, 59, 255)
SCREEN = (71, 105, 133, 255)
GLASS = (148, 187, 208, 255)
AMBER = (242, 193, 78, 255)

d.rounded_rectangle([10, 10, 246, 246], radius=44, fill=NAVY)

# left monitor
d.rounded_rectangle([26, 92, 96, 164], radius=10, fill=SCREEN)
d.rectangle([34, 100, 88, 156], fill=GLASS)
d.rectangle([48, 164, 74, 178], fill=SCREEN)
# right monitor
d.rounded_rectangle([160, 92, 230, 164], radius=10, fill=SCREEN)
d.rectangle([168, 100, 222, 156], fill=GLASS)
d.rectangle([182, 164, 208, 178], fill=SCREEN)

# top arrow: to the right
d.line([102, 112, 138, 112], fill=AMBER, width=10)
d.polygon([(136, 100), (158, 112), (136, 124)], fill=AMBER)
# bottom arrow: to the left
d.line([154, 152, 118, 152], fill=AMBER, width=10)
d.polygon([(120, 140), (98, 152), (120, 164)], fill=AMBER)

img.save(
    "icon.ico",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
img.save("icon_preview.png")
print("icon.ico written")
