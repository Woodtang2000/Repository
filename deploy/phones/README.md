# Phones: where every number goes (as of 11/16/2026)

Donna (Twilio): (866) 827-8398 customer line (text + voice), (888) 519-7604 employee line. During office hours she
transfers service calls to the service line (OFFICE_PHONE, default +19078022449) and Bill / a manager / sales to Bill
(SALES_PHONE, default +19075190752). Push changes to ElevenLabs with `python -m service_changes.phone_agent --setup`.

Vonage Business (account 313154):
- Service line (907) 802-2449, call group 503: Sonja (404) + Ana (406) ring together 25 s, then the group voicemail,
  emailed to Sonja, Ana and Bill. No menu.
- Bill (907) 519-0752, ext 403: Follow Me, Bill 20 s, then Scott (410) 15 s, then Bill's voicemail.
- Menus (virtual receptionists): Snow White Main 519-0762 (1 service group, 2 Bill, 3-4 Sonja), TLG Main 519-0761
  (1 ext 420, 2-4 Bill), Dondees 802-2418 / 802-2447 (rings 414, then 414 voicemail to Service@dondeeslaundry.com).
- Ext 420 "TLG Service Desk": the TLG plant-floor desk phones (Front Counter, Garment Sort, Clean Room). Keep.
- Kept licenses: 403 Bill, 404 Sonja, 406 Ana, 410 Scott, 414 Dondees, 420 TLG plant, 501 Esa.
- Case 12000360 (11/16): remove licenses 412 413 415 416 421 402 502 409 (15 -> 7), release 802-2470 519-0751
  802-2425 802-2380 802-2437 802-2438 802-2421 519-0759 519-0755, explain 8 billed Local Company Numbers vs 6, credit
  any billed in error. Expected bill about $305/month (was $604).
- (907) 830-4952 (Will Woodland's personal phone) removed from the account 11/16.

Legacy Alaska Communications (ACS) lines, forwarding only; plan is to port them to Twilio and Donna:
(907) 222-7738 Snow White original (website "Main"), (907) 258-4200 Alaska Cleaners -> TLG Main, (907) 279-0251
Dondees Laundromat. (907) 868-9410 (old "24hr hotline") goes nowhere: removed from the website 11/16.

Twilio toll-free verifications (both approved): HH7e0ec402eb1b0f799421e4618756b366 (866) 827-8398 and
HHbba8f80da2087f4e039a4a24e82aa886 (888) 519-7604. Their use case covers replies only; Twilio ticket #30401022
(11/16) asks to add service notices (holiday changes, "we're open", repair updates), name The Laundry Group as an
affiliate, replace the samples and point both opt-in proofs at snowwhitelinen.com/sms-terms. Asked first whether an
update pauses texting; if so, hold until after 11/27. Texts Donna starts end with the STOP line (announce.with_stop).
