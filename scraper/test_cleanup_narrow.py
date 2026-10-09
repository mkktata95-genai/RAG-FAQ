import content_freshness_httpV1 as cf, scrape_approved_urls_httpV1 as sc
md = '''1. [Home](/ "home")
   >

2. [Existing customers](/existing-customers/)
   >

3. [Manage your pension](/x/)
   >

4. Access your pension

# Access your pension

Steps:

1. Log in to your account
2. Check your details

## About Royal London

Real note to editors text.

**The Royal London Mutual Insurance Society Limited** is real legal text.
'''
a, b = cf.clean_scraped_content(md), sc.clean_content(md)
assert a == b
assert "[Home]" not in a and "4. Access your pension" not in a
assert "1. Log in to your account" in a and "2. Check your details" in a
assert "Real note to editors" in a and "Mutual Insurance Society" in a
print(a); print("OK")