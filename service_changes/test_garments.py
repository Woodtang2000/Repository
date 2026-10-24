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
