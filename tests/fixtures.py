"""Synthetic statement text in the layouts real issuers use.

These mirror the shapes that PDF text extraction produces: a table of labels
above a row of values, or plain inline label-value pairs.
"""

import pikepdf

STATEMENT_LINES = [
    "HDFC Bank Credit Card Statement",
    "Card No: 4321 XXXX XXXX 8765",
    "Total Amount Due 45,231.50",
    "Minimum Amount Due 2,270.00",
    "Payment Due Date 25/07/2026",
]

# The same statement with its line items, as a text-only PDF prints them: a
# date, the merchant, the amount, and sometimes a reward points column.
STATEMENT_WITH_TRANSACTIONS = STATEMENT_LINES + [
    "Statement Date 05/07/2026",
    "Domestic Transactions",
    "02/07/2026 SWIGGY BANGALORE 640.00 12",
    "03/07/2026 04/07/2026 UBER INDIA SYSTEMS 249.50",
    "04/07/2026 CROMA RETAIL WHITEFIELD 8,499.00",
    "05/07/2026 PAYMENT RECEIVED THANK YOU 5,000.00 Cr",
]

# What pdfplumber hands back from a statement that rules its transactions into a
# grid and names the columns, category included.
TRANSACTION_TABLE = [
    [
        ["Transaction Date", "Transaction Details", "Category", "Amount (INR)"],
        ["12/06/2026", "ZOMATO ONLINE ORDER", "Food & Beverages", "480.00"],
        ["14/06/2026", "MYNTRA DESIGNS", None, "2,145.00"],
        ["15/06/2026", "REFUND MYNTRA DESIGNS", "Shopping", "500.00 Cr"],
        ["", "Total", "", "2,125.00"],
    ]
]


def write_pdf(path, *, password: str | None = None, lines: list[str] | None = None) -> None:
    """Build a small text PDF, optionally locked."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    body = "\n".join(f"({line}) Tj 0 -18 Td" for line in (lines or STATEMENT_LINES))
    content = pdf.make_stream(f"BT /F1 12 Tf 40 740 Td {body} ET".encode())
    page = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=[0, 0, 612, 792],
            Contents=content,
            Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
        )
    )
    pdf.pages.append(pikepdf.Page(page))
    if password:
        pdf.save(path, encryption=pikepdf.Encryption(user=password, owner=password))
    else:
        pdf.save(path)

HDFC_TABLE = """
HDFC Bank Credit Card Statement
Name: NAVEEN KUMAR
Card No: 4321 XXXX XXXX 8765

Statement Date Payment Due Date Total Dues Minimum Amount Due
05/07/2026 25/07/2026 45,231.50 2,270.00

Credit Limit Available Credit Limit Available Cash Limit
5,00,000.00 4,54,768.50 1,00,000.00

Domestic Transactions
02/07/2026 SWIGGY BANGALORE 640.00
"""

ICICI_INLINE = """
ICICI Bank Credit Card Statement

Card Number: 5241 XXXX XXXX 4409
Statement Date : 18-07-2026
Payment Due Date : 05-08-2026
Total Amount due : Rs. 1,12,480.35
Minimum Amount due : Rs. 5,624.00
Credit Limit : Rs. 8,00,000.00
Available Credit Limit : Rs. 6,87,519.65
"""

SBICARD_TABLE = """
SBI Card Statement
Statement Date: 12 Jul 2026

Total Amount Due Minimum Amount Due Payment Due Date
INR 23,908.00 INR 1,200.00 02 Aug 2026

Credit Limit: INR 3,50,000.00
Available Credit Limit: INR 3,26,092.00
Card Number 4173 XXXX XXXX 1122
"""

AXIS_INLINE = """
Axis Bank Credit Card

Statement Period: 15-06-2026 to 14-07-2026
Statement Date 14-07-2026
Total Payment Due
Rs. 67,540.20
Minimum Payment Due
Rs. 3,377.00
Payment Due Date
03-08-2026
Card No: XXXX XXXX XXXX 7788
"""

AMEX_INLINE = """
American Express
Prepared for NAVEEN KUMAR
Account Number XXXX XXXXXX 91004

Closing Date July 20, 2026
New Balance $ 0.00
New Balance 1,84,220.75
Minimum Payment Due 9,211.00
Payment Due Date August 8, 2026
Total Credit Limit 12,00,000.00
"""

CREDIT_BALANCE = """
Kotak Mahindra Bank Credit Card Statement
Statement Date: 01-07-2026
Payment Due Date: 20-07-2026
Total Amount Due: 3,410.75 Cr
Minimum Amount Due: 0.00
Card Number: 4021 XXXX XXXX 5150
"""

UNKNOWN_ISSUER = """
Sunrise Co-operative Bank Card Services

Statement Date 09-07-2026
Payment Due Date 29-07-2026
Total Amount Due 8,750.00
Minimum Amount Due 440.00
Card Number 6011 XXXX XXXX 3344
"""

NOT_A_STATEMENT = """
Dear customer, your credit card application has been received.
We will contact you within 7 working days.
"""
