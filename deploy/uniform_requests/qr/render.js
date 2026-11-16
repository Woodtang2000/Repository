const {chromium}=require('playwright');const path=require('path');
(async()=>{const b=await chromium.launch();for(const k of ['sw','tlg']){const p=await b.newPage({viewport:{width:816,height:1056},deviceScaleFactor:2});
await p.goto('file://'+path.join(__dirname,`sign_${k}.html`));await p.pdf({path:path.join(__dirname,`Uniform-Repair-Sign-${k.toUpperCase()}.pdf`),format:'Letter',printBackground:true});
await p.screenshot({path:path.join(__dirname,`Uniform-Repair-Sign-${k.toUpperCase()}.png`)});}await b.close();})();
