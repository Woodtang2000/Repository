"""QR codes for the garment inventory page, and a printable sign per company (Scott, 11/22). Same design as the repair
 signs (deploy/uniform_requests/qr). python3 make_qr.py -> qr_sw/qr_tlg .svg/.png and sign_sw/sign_tlg.html (render.js)"""
import base64, pathlib, segno
HERE = pathlib.Path(__file__).parent
BRANDS = {
    "sw": {"url": "https://snowwhitelinen.com/inventory", "show": "snowwhitelinen.com/inventory", "navy": "#003746",
           "gold": "#f9af10", "logo": "logo_sw.png", "name": "Snow White Linen",
           "head": "Count your garments", "what": "Help us keep your garment count right"},
    "tlg": {"url": "https://thelaundrygroup.com/inventory", "show": "thelaundrygroup.com/inventory", "navy": "#003f50",
            "gold": "#ffb807", "logo": "logo_tlg.png", "name": "The Laundry Group",
            "head": "Count your garments", "what": "Help us keep your garment count right"},
}
for key, b in BRANDS.items():
    qr = segno.make(b["url"], error="m")
    qr.save(HERE / f"qr_{key}.svg", scale=10, border=4, dark=b["navy"], light="#ffffff")
    qr.save(HERE / f"qr_{key}.png", scale=40, border=4, dark=b["navy"], light="#ffffff")
    svg = (HERE / f"qr_{key}.svg").read_text()
    logo = base64.b64encode((HERE.parent.parent.parent / "brand" / b["logo"]).read_bytes()).decode()
    html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: letter; margin: 0 }}
* {{ box-sizing: border-box }}
body {{ margin: 0; width: 816px; height: 1056px; font-family: Inter, "Helvetica Neue", Arial, sans-serif; color: #1c2226 }}
.top {{ height: 14px; background: {b["gold"]} }}
.wrap {{ padding: 40px 64px 0; text-align: center }}
.logo {{ height: 80px }}
h1 {{ font-size: 48px; line-height: 1.1; margin: 26px 0 10px; color: {b["navy"]}; letter-spacing: -.02em }}
.what {{ font-size: 20px; color: #4a565e; margin: 0 0 24px }}
.qr {{ display: inline-block; padding: 6px; border: 5px solid {b["navy"]}; border-radius: 28px }}
.qr svg {{ width: 330px; height: 330px; display: block }}
.scan {{ font-size: 30px; font-weight: 700; color: {b["navy"]}; margin: 18px 0 2px }}
.url {{ font-size: 22px; color: #4a565e; margin: 0 0 24px }}
.steps {{ display: flex; gap: 18px; justify-content: center; margin: 0 0 30px }}
.step {{ flex: 1; background: #f2f5f6; border-radius: 16px; padding: 18px 14px; font-size: 18px; line-height: 1.35 }}
.step b {{ display: block; font-size: 30px; color: {b["gold"]}; margin-bottom: 4px }}
.foot {{ position: absolute; left: 0; right: 0; bottom: 0; background: {b["navy"]}; color: #fff; text-align: center;
         padding: 20px 32px; font-size: 19px }}
.foot strong {{ white-space: nowrap }}
.foot strong {{ color: {b["gold"]} }}
</style></head><body><div class="top"></div><div class="wrap">
<img class="logo" src="data:image/png;base64,{logo}" alt="{b["name"]}">
<h1>{b["head"]}</h1>
<p class="what">{b["what"]}. It takes a few minutes on your phone.</p>
<div class="qr">{svg[svg.index("<svg"):]}</div>
<p class="scan">Scan to count your garments</p>
<p class="url">{b["show"]}</p>
<div class="steps">
<div class="step"><b>1</b>Scan the QR code and type your name</div>
<div class="step"><b>2</b>Scan the barcode label on each garment you have</div>
<div class="step"><b>3</b>Tap Send. Your garment report updates within a business day</div>
</div></div>
<div class="foot">It doesn't change anything about your garments. Questions? Text <strong>866-TAP-TEXT</strong></div>
</body></html>"""
    (HERE / f"sign_{key}.html").write_text(html)
print("ok")
