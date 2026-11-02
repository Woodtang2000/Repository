from . import notebook


def test_the_notebook_keeps_screen_know_how_only(tmp_path):
    d = str(tmp_path)
    assert notebook.read(d) == ""
    assert notebook.add(d, "Special Invoice screen: the Customer box starts with 1000; clear it first.") == "saved"
    assert notebook.add(d, "special invoice screen:  the customer box starts with 1000; clear it first") == "already in the notebook"
    assert notebook.add(d, "Chili's owes $1,240") .startswith("not saved")
    assert notebook.add(d, "ok") == "too short"
    assert notebook.read(d).count("\n") == 1 and "starts with 1000" in notebook.read(d)
