"""The merged app must register exactly the four functions (guards a dropped or duplicated trigger in the merge)."""


def test_function_app_registers_the_four_functions():
    import function_app
    names = sorted(f.get_function_name() for f in function_app.app.get_functions())
    assert names == ["freshness_daily", "freshness_on_request", "scrape_on_excel", "scrape_on_request"]
