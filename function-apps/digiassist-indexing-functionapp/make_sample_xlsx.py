"""Writes sample_templated.xlsx (the Excel layout). Usage: python make_sample_xlsx.py"""
from openpyxl import Workbook
H = ["id", "question", "answer", "intent", "scenario", "advice_boundary", "active",
     "citation_1_title", "citation_1_url", "citation_2_title", "citation_2_url", "citation_3_title", "citation_3_url",
     "cta_1_label", "cta_1_url", "cta_2_label", "cta_2_url", "cta_3_label", "cta_3_url"]
R = [
    ["", "What is a pension?", "A pension is a long-term savings plan that helps you put money aside for retirement.", "", "", "", "Y",
     "Pensions explained", "https://www.royallondon.com/guides-tools/pension-guides/pension-basics/pensions-explained/", "", "", "", "",
     "Pension basics", "https://www.royallondon.com/guides-tools/pension-guides/pension-basics/", "", "", "", ""],
    ["", "Are pensions invested?", "Most pensions are invested, so their value can go up and down over time.", "", "", "", "Y",
     "Are pensions invested", "https://www.royallondon.com/guides-tools/pension-guides/pension-basics/are-pensions-invested/", "", "", "", "",
     "Pension investments", "https://www.royallondon.com/guides-tools/pension-guides/pension-investments/", "", "", "", ""],
    ["", "How do I make a complaint?", "You can raise a complaint online, by phone or by post, and it will be handled under the complaints policy.", "", "", "", "Y",
     "Make a complaint", "https://www.royallondon.com/existing-customers/help-and-support/make-a-complaint/", "", "", "", "",
     "Complaints policy", "https://www.royallondon.com/existing-customers/help-and-support/make-a-complaint/complaints-policy/", "", "", "", ""],
]
wb = Workbook(); ws = wb.active; ws.title = "questions"; ws.append(H)
for r in R: ws.append(r)
wb.save("sample_templated.xlsx"); print("wrote sample_templated.xlsx")
