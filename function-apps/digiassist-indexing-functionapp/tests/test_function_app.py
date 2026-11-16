"""The merged app must register exactly the four functions (a missing one fails silently in Azure)."""
import function_app


def test_registered_functions():
    names = sorted(f.get_function_name() for f in function_app.app.get_functions())
    assert names == ["index_on_request", "index_on_scrape", "templated_on_request", "templated_on_upload"]
