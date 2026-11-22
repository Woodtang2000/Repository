# Inventory home-screen icon: a gold clipboard with check marks on navy, the same style as the Repairs shirt
# (deploy/uniform_requests/make_icon.py). Run from the repo root.
from PIL import Image, ImageDraw
S = 4; W = 512 * S
NAVY = (0, 55, 70); GOLD = (249, 175, 16)
def P(pts): return [(x * S, y * S) for x, y in pts]
img = Image.new("RGB", (W, W), NAVY); d = ImageDraw.Draw(img)
d.rounded_rectangle(P([(132, 104), (380, 424)]), radius=28 * S, fill=GOLD)          # the board
d.rounded_rectangle(P([(196, 80), (316, 132)]), radius=16 * S, fill=NAVY)           # the clip
d.rounded_rectangle(P([(208, 90), (304, 122)]), radius=10 * S, fill=GOLD)
for y in (190, 262, 334):                                                           # three checked lines
    d.line(P([(170, y), (190, y + 20), (226, y - 18)]), fill=NAVY, width=14 * S, joint="curve")
    d.rounded_rectangle(P([(246, y - 8), (344, y + 10)]), radius=9 * S, fill=NAVY)
img.resize((512, 512), Image.LANCZOS).save("deploy/inventory/inventory_icon.png")
