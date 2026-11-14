# Uniform Requests home-screen icon: navy and gold like Donna's (website_chat/donna_icon.png). Run from the repo root.
from PIL import Image, ImageDraw, ImageFilter
S=4; W=512*S
NAVY=(0,55,70); GOLD=(249,175,16)
def P(pts): return [(x*S,y*S) for x,y in pts]
mask=Image.new("L",(W,W),0); d=ImageDraw.Draw(mask)
d.polygon(P([(204,104),(146,126),(78,214),(124,258),(162,228),(162,410),(350,410),(350,228),(388,258),(434,214),(366,126),(308,104)]),fill=255)
mask=mask.filter(ImageFilter.GaussianBlur(9*S)).point(lambda v:255 if v>128 else 0)
img=Image.new("RGB",(W,W),NAVY); img.paste(GOLD,(0,0),mask)
d=ImageDraw.Draw(img)
# V neck and collar points
d.polygon(P([(204,104),(256,176),(308,104)]),fill=NAVY)
lw=10*S
d.line(P([(204,104),(226,168),(256,176)]),fill=NAVY,width=lw,joint="curve")
d.line(P([(308,104),(286,168),(256,176)]),fill=NAVY,width=lw,joint="curve")
# placket
d.line(P([(256,176),(256,410)]),fill=NAVY,width=8*S)
for y in (226,280,334):
    r=12*S; d.ellipse((256*S-r,y*S-r,256*S+r,y*S+r),fill=NAVY)
# chest pocket
d.rounded_rectangle(P([(290,226),(334,268)]),radius=6*S,outline=NAVY,width=8*S)
img=img.resize((512,512),Image.LANCZOS)
img.save("deploy/uniform_requests/uniform_icon.png")
