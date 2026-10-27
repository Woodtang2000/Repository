from datetime import datetime

from .context import ALASKA, Alliant, garment_facts


def write(tmp_path):
    (tmp_path / "customers.csv").write_text("account,name,route,service_days\n1205-1-00007,MIDAS 7,4,Tue\n")
    (tmp_path / "wearers.csv").write_text("account,employee,first,last,stopped_date\n"
                                          "1205-1-00007,12,Joey,Smith,\n1205-1-00007,13,Ann,Lee,\n")
    (tmp_path / "garments_in_service.csv").write_text(
        "barcode,cust_code,branch,wearer,sku,size_a,size_b,in_service_date,washes,repairs,status,status_date,last_delivered,scan_text,scan_date,description\n"
        "100001,1205-1-00007,1,12,2-01-01,L,,2026-10-01,3,0,U,2026-10-21,2026-10-21,MIDAS 4,2026-10-21 10:15:00,SHT NAVY L/S\n"
        "100002,1205-1-00007,1,12,2-21-01,34,32,2026-10-01,3,0,I,2026-10-22,2026-10-14,,,PNT NAVY\n"
        "100003,1205-1-00007,1,13,2-21-01,30,30,2026-10-01,3,0,O,2026-10-22,2026-10-21,,,PNT NAVY\n")
    (tmp_path / "purchase_orders.csv").write_text(
        "po,po_date,line,sku,description,size_a,size_b,cust_code,wearer,qty_ordered,qty_received,received_date,expected_date,vendor_name\n"
        "900,2026-10-01,1,2-01-01,SHT NAVY L/S,L,,1205-1-00007,12,11,5,2026-10-10,2026-10-30,Workwear Outfitters\n"
        "16,2006-08-08,1,2-01-01,SHT NAVY L/S,3XL,L,1205-1-00007,12,11,5,,,Dickies\n")
    (tmp_path / "feed_done.txt").write_text("2026-10-23T03:50:00-0800\n")


def test_garment_facts_for_a_named_wearer(tmp_path):
    write(tmp_path)
    a = Alliant.from_dir(str(tmp_path))
    assert len(a.orders["1205-1-00007"]) == 1  # the 2006 line is a dead order
    f = garment_facts(a, "1205-1-00007", "where are Joey's garments?")
    assert 'custom scan "MIDAS 4" 2026-10-21 10:15:00' in f and "in soil (back at the plant)" in f
    assert "100003" not in f  # only Joey in detail...
    assert "#13 Ann Lee: 1 garments (1 out clean" in f  # ...the others summarized
    assert "PO 900 2026-10-01, wearer #12 Joey Smith: SHT NAVY L/S L: received 5 of 11 on 2026-10-10 (OPEN, expected 2026-10-30)" in f
    assert "price" not in f.lower()


def test_small_account_without_a_name_shows_everyone(tmp_path):
    write(tmp_path)
    f = garment_facts(Alliant.from_dir(str(tmp_path)), "1205-1-00007", "any garments stuck?")
    assert "100003" in f and "100001" in f
    assert garment_facts(Alliant.from_dir(str(tmp_path)), "9999-1-00000") == ""


def test_office_group_questions():
    from types import SimpleNamespace
    from .context import Customer, group_facts
    from .desk import Desk
    a = Alliant(customers=[Customer("4000-1-00001", "SAFEWAY 1805 DELI", "3", ["Mon"]),
                           Customer("4000-1-00002", "SAFEWAY 1805 MEAT", "3", ["Mon"]),
                           Customer("1205-1-00007", "MIDAS 7", "4", ["Tue"])],
                items={"4000-1-00001": {"MAT 3X5": 4}, "4000-1-00002": {"MAT 3X5": 2, "MAT 4X6": 1}})
    d = SimpleNamespace(alliant=a)
    p = lambda acct=None, name=None: SimpleNamespace(account_number=acct, customer_as_written=name)
    g = lambda text, parsed: Desk.office_group(d, text, parsed)
    assert g("how many mats does account 4000 have on inventory?", p()) == ["4000-1-00001", "4000-1-00002"]
    assert g("4000, all of the customers in 4000", p("4000-1-00001")) == ["4000-1-00001", "4000-1-00002"]
    assert g("mat inventory for every safeway account", p(name="safeway")) == ["4000-1-00001", "4000-1-00002"]
    assert g("how many mats does Safeway 1805 deli get?", p("4000-1-00001", "Safeway 1805 deli")) == []
    assert g("does Midas 7 get 1205 towels?", p("1205-1-00007")) == []
    f = group_facts(a, ["4000-1-00001", "4000-1-00002"])
    assert "2 accounts" in f and "4000-1-00002 SAFEWAY 1805 MEAT (route 3): MAT 3X5 2/delivery inv 2; MAT 4X6 1/delivery inv 1" in f


def test_route_facts():
    from .context import Customer, route_facts
    a = Alliant(customers=[Customer("1205-1-00007", "MIDAS 7", "4", ["Mon", "Thu"]),
                           Customer("1300-1-00000", "KOBUK COFFEE", "4", ["Thu"]),
                           Customer("1400-1-00000", "OTHER", "5", ["Thu"])],
                cards={"1205-1-00007": {"stop_sequence": "Mon:5;Thu:22"}, "1300-1-00000": {"stop_sequence": "Thu 3"}},
                frequency={"1300-1-00000": {"MAT 3X5": "5", "BAR MOP": "7"}})
    from datetime import datetime
    from .context import ALASKA
    a.customers.append(Customer("5900-2-00000", "WESTMARK HOTEL", "4", ["Thu"]))  # TLG's own route 4
    a.cards["5900-2-00000"] = {"stop_sequence": "Thu 1"}
    f = route_facts(a, "4", datetime(2026, 10, 29, 8, tzinfo=ALASKA))  # a Thursday
    assert f.startswith("Today is Thursday October 29, 2026 (Thu).") and "OTHER" not in f
    assert "Snow White Linen route 4: 2 customers" in f and "The Laundry Group route 4: 1 customers" in f
    assert "- 1300-1-00000 KOBUK COFFEE: Thu; stops Thu 3; MAT 3X5 every other week" in f and "BAR MOP" not in f
    assert ("TODAY (Thu): 2 stops; first 1300-1-00000 KOBUK COFFEE (stop 3), last 1205-1-00007 MIDAS 7 (stop 22). "
            "Order: 3. 1300-1-00000 KOBUK COFFEE, 22. 1205-1-00007 MIDAS 7") in f
    assert "Mon: 1 stops; first 1205-1-00007 MIDAS 7 (stop 5)" in f
    assert "TODAY (Thu): 1 stops; first 5900-2-00000 WESTMARK HOTEL" in f  # never mixed with Snow White's stop order
    assert route_facts(a, "9") == ""


def test_sku_details_without_prices(tmp_path):
    from .context import sku_facts
    (tmp_path / "skus.csv").write_text("sku,description,weight_oz,size,unit_price,replacement_cost\n"
                                       "6-01-05,BATH BLANKET WHITE,28,27x52,4.10,12.00\n7-00-07,BAR MOP,2,16x19,0.30,1.00\n")
    a = Alliant()
    a.load_skus(str(tmp_path / "skus.csv"))
    f = sku_facts(a, "how much does a bath blanket weigh?")
    assert f == "SKU 6-01-05: description BATH BLANKET WHITE; weight 28 oz (= 1.75 lb); size 27x52"  # no price or cost
    assert sku_facts(a, "what is 7-00-07").startswith("SKU 7-00-07: description BAR MOP")


def test_barcode_facts(tmp_path):
    write(tmp_path)
    (tmp_path / "garment_scans.csv").write_text(
        "barcode,cust_code,wearer,status,scan_datetime,scan_text,scan_type,route\n"
        "100001,1205-1-00007,12,I,2026-10-17 06:00:00,,Count Station,4\n"
        "100001,1205-1-00007,12,O,2026-10-20 05:00:00,,Count Station,4\n"
        "100001,1205-1-00007,12,U,2026-10-21 10:15:00,MIDAS 4,Handheld,4\n")
    from .context import barcode_facts
    a = Alliant.from_dir(str(tmp_path))
    f = barcode_facts(a, ["100001", "999"])
    assert f.startswith('Garment 100001 SHT NAVY L/S L: custom scan "MIDAS 4" 2026-10-21 10:15:00; 3 washes; in service since 2026-10-01; last delivered 2026-10-21')
    assert "Belongs to: 1205-1-00007 MIDAS 7, wearer #12 Joey Smith" in f
    lines = f.splitlines()
    assert lines[3] == '  - 2026-10-21 10:15:00: custom scan "MIDAS 4" (Handheld)' and "in soil" in lines[5]


def test_garment_check_flags_extra_inventory_with_nothing_on_order(tmp_path):
    from .garment_check import findings
    write(tmp_path)
    (tmp_path / "garments.csv").write_text(
        "account,employee,sku,size,item,quantity,autocount\n"
        "1205-1-00007,12,2-01-01,L,SHT NAVY L/S,11,5\n"    # 6 extra, PO 900 has 6 open: waiting, fine
        "1205-1-00007,12,2-21-01,34 32,PNT NAVY,11,5\n"    # 6 extra, nothing on order: flag
        "1205-1-00007,13,2-21-01,30 30,PNT NAVY,11,11\n"  # matches
        "1205-1-00007,14,2-21-01,32 30,PNT NAVY,22,11\n")  # double: the ratio rule allows it
    [f] = findings(str(tmp_path))
    assert (f["wearer"], f["name"], f["item"], f["extra"], f["on_order"], f["barcodes_in_service"]) == \
        ("12", "Joey Smith", "PNT NAVY", 6, 0, 1)
    assert "doesn't match the auto count 5" in f["issue"]


def test_weekly_stockroom_post(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from . import desk
    write(tmp_path)
    (tmp_path / "garments.csv").write_text("account,employee,sku,size,item,quantity,autocount\n"
                                           "1205-1-00007,12,2-21-01,34 32,PNT NAVY,11,5\n"
                                           "1205-1-00007,13,4-01-01,L,NOG GARMENTS,3,0\n")  # customer-owned: skipped
    posts = []

    def post(cid, text, payload, **kw):
        posts.append((cid, text, kw))
        return {"ts": str(len(posts))}
    d = SimpleNamespace(channels={"stockroom": "CSR"}, data_dir=str(tmp_path), alliant=Alliant.from_dir(str(tmp_path)),
                        slack=None, stockroom_lead="UANA", post=post)
    monday_8am = datetime(2026, 10, 26, 8, 0, tzinfo=ALASKA).timestamp()
    desk.Desk.stockroom_pass(d, monday_8am - 3600 * 2)  # before 7: nothing
    assert posts == []
    desk.Desk.stockroom_pass(d, monday_8am)
    desk.Desk.stockroom_pass(d, monday_8am + 600)  # once a week
    (cid, head, _), (_, body, kw), (_, end, _) = posts
    assert cid == "CSR" and head.startswith("🧺 <@UANA> *Weekly garment check*") and "*1 wearer line* at *1 account*" in head
    assert kw == {"thread_ts": "1"} and body.startswith("*1205-1-00007* MIDAS 7\n")
    assert "#12 Joey Smith · PNT NAVY 34 32: inventory 11, auto count 5" in body and "NOG" not in body
    assert "customer-owned (NOG) garments" in end
    desk.Desk.stockroom_pass(d, monday_8am + 86400)  # Tuesday: nothing
    assert len(posts) == 3


def test_garment_check_skips_tlg_pools_and_flags_accounts_never_set_up(tmp_path):
    from .garment_check import findings
    write(tmp_path)
    (tmp_path / "garments.csv").write_text(
        "account,employee,sku,size,item,quantity,autocount,price_type\n"
        "6000-2-00000,2,7-600NTJ,S,MEDICAL SCRUB PANT JADE,798,0,\n"   # TLG scrub pool: skipped
        "1300-1-00000,4,2-CT10NV,44 R,COVERALL NAVY,6,0,11\n"        # price type 11: COG, skipped
        "5026-1-00003,1,2-CT10NV,44 R,COVERALL,2,0,\n"   # every line 0: never set up, or COG not marked yet
        "5026-1-00003,2,2-CT10NV,46 R,COVERALL,2,0,\n"
        "7487-1-00004,1,2-PT2ABK,36x30,PANT INDUSTRIAL SHOP,11,0,\n"  # Alyeska Tire: on the COG list, skipped
        "1205-1-00007,12,2-21-01,34 32,PNT NAVY,11,5,9\n")
    rows = findings(str(tmp_path))
    assert {r["account"] for r in rows} == {"5026-1-00003", "1205-1-00007"}
    assert [r["auto_count_unset"] for r in rows if r["account"] == "5026-1-00003"] == [True, True]
    assert [r["auto_count_unset"] for r in rows if r["account"] == "1205-1-00007"] == [False]
