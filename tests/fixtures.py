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
Name: ADA LOVELACE
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

# Amazon Pay ICICI PDFs often paint each header letter twice. Without undoubling,
# the real due date is missed and a sample date from the T&Cs is taken instead.
ICICI_AMAZON_DOUBLED_HEADERS = """
ICICI Bank Credit Card Statement
MR. ADA LOVELACE
SSTTAATTEEMMEENNTT DDAATTEE
July 28, 2026
PPAAYYMMEENNTT DDUUEE DDAATTEE
August 15, 2026
STATEMENT SUMMARY
Total Amount due
`7,317.00
Minimum Amount due
`370.00
Credit Limit (Including cash) Available Credit (Including cash)
`6,70,000.00 `6,62,180.00
4315XXXXXXXX4019

Interest calculation
2 Total Amount Due on statement dated Oct 08, 2023 2,000.00
3 Minimum Amount Due on statement dated Oct 08, 2023 100.00
4 Payment due date - Oct 26, 2023
5 Payment due date: Oct 26, 2025
"""

# When the header Statement Date is missing, the period end is the bill date.
ICICI_PERIOD_ONLY = """
ICICI Bank Credit Card Statement
Statement Period: June 29, 2026 to July 28, 2026
Payment Due Date : 15-08-2026
Total Amount due : Rs. 7,317.00
Minimum Amount due : Rs. 370.00
Card Number: 4315 XXXX XXXX 4019
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

# Cashback PDFs put the statement number between the MAD label and the amount.
SBICARD_CASHBACK_STMT_NO = """
SBI Card Statement
*Total Amount Due ( ` )
301.00
PLACE OF SUPPLY : MAD/23/MADHYA PRADESH
**Minimum Amount Due( ` )
STMT No. : A25122229867
200.00
Payment Due Date
02/01/2026
Card Number 4375 XXXX XXXX 9326
"""

# Header Statement Date must beat Statement Period + following transaction dates.
SBICARD_CASHBACK_HEADER = """
SBI Card Statement
*Total Amount Due ( ` )
301.00
**Minimum Amount Due( ` )
STMT No. : A25122229867
200.00
Credit Limit( ` ) (including cash) Cash Limit( ` )(as part of credit limit) Statement Date
30,000.00 9,000.00 24 Dec 2025
Available Credit Limit ( ` ) Available Cash Limit ( ` ) Payment Due Date
29,698.97 9,000.00 13 Jan 2026
Date Transaction Details Amount ( ` )
for Statement Period: 25 Nov 25 to 24 Dec 25
24 Nov 25 CARD CASHBACK CREDIT 467.00 C
28 Nov 25 PAYMENT RECEIVED 000DP11533209091391F7R2 8,462.00 C
Card Number XXXX XXXX XXXX 9326
"""

# Demat e-statements arrive from the same bank domain and must not become cards.
ICICI_DEMAT_ESTATEMENT = """
ICICI Bank
Transaction e-Statement for ICICI Bank Demat Account IN303028 XXXXXX95
Total Amount Due 100.00
Account Number 81000229118968
By CM ICICI SECURITIES LIMITED 2,026,073.00
"""

# Tata Neu Infinity: compact mask is the card; alternate account is not.
HDFC_TATA_NEU_COMPACT = """
HDFC Bank Credit Card Statement
Tata Neu Infinity
Credit Card No. 652926XXXXXX2750
Alternate Account Number 0001010410000722758
Statement Date 01/04/2026
Payment Due Date 20/04/2026
Total Amount Due 12,345.00
Minimum Amount Due 617.00
"""

# Exact live layout from Tata Neu Infinity July-2026 PDF text extraction.
HDFC_TATA_NEU_LIVE_LAYOUT = """
Tata Neu Infinity HDFC Bank Credit Card Statement
HSN Code: 997113 HDFC Bank Credit Cards GSTIN: 33AAACH2702H2Z6
ADA LOVELACE Credit Card No. 652926XXXXXX2750
F3 Subhiksha Habitat Apartment Kaggadaspura Alternate Account Number 0001010410000722758
Statement Date 01 Jul, 2026
PREVIOUS STATEMENT DUES FINANCE CHARGES TOTAL AMOUNT DUE
_ C4,373.00
TOTAL CREDIT LIMIT
(Including Cash) AVAILABLE CREDIT LIMIT AVAILABLE CASH LIMIT MINIMUM DUE DUE DATE
C220.00 21 Jul, 2026
C3,98,000 C3,93,627 C1,59,200
Past Dues OVER LIMIT 3 MONTHS + 2 MONTHS 1 MONTH CURRENT DUES MINIMUM DUES
(if any) C0.00 C0.00 C0.00 C0.00 C220.00 C220.00
"""

# Swiggy HDFC uses the same fused MINIMUM DUE DUE DATE row.
HDFC_SWIGGY_LIVE_LAYOUT = """
Swiggy HDFC Bank Credit Card Statement
Ada Lovelace Credit Card No. 526873XXXXXX6527
Alternate Account Number 0001010610012476521
Statement Date 20 Jul, 2026
PREVIOUS STATEMENT DUES FINANCE CHARGES TOTAL AMOUNT DUE
_ C2,447.00
TOTAL CREDIT LIMIT
(Including Cash) AVAILABLE CREDIT LIMIT AVAILABLE CASH LIMIT MINIMUM DUE DUE DATE
C200.00 09 Aug, 2026
C3,98,000 C3,95,226 C1,59,200
"""

# Older Tata Neu PDFs put T&C "total amount due" / "minimum amount due" on one
# line, with the HDFC ERGO Toll Free (+800 08250825) on the next — that must
# never become total_due 82,50,825 / min_due 800.
HDFC_TATA_NEU_TC_PHONE = """
Tata Neu Infinity HDFC Bank Credit Card Statement
Credit Card No. 652926XXXXXX2750
Statement Date 01 May, 2026
PREVIOUS STATEMENT DUES FINANCE CHARGES TOTAL AMOUNT DUE
RECEIVED (Current Billing Cycle)
_ C1,72,729.00
TOTAL CREDIT LIMIT
(Including Cash) AVAILABLE CREDIT LIMIT AVAILABLE CASH LIMIT MINIMUM DUE DUE DATE
C8,640.00 21 May, 2026
C3,98,000 C2,25,271 C1,59,200
Past Dues OVER LIMIT 3 MONTHS + 2 MONTHS 1 MONTH CURRENT DUES MINIMUM DUES
(if any) C0.00 C0.00 C0.00 C0.00 C8,640.00 C8,640.00
l If the minimum amount due or part amount less than the total amount due is paid, interest charges are applicable.
Insurance provider: HDFC ERGO. Toll Free: +800 08250825/01204507250 (Chargeable)
"""

# Same layout with the dial string missing its leading zero — still not a bill.
HDFC_TATA_NEU_TC_PHONE_NO_LEADING_ZERO = """
Tata Neu Infinity HDFC Bank Credit Card Statement
Credit Card No. 652926XXXXXX2750
Statement Date 01 May, 2026
PREVIOUS STATEMENT DUES FINANCE CHARGES TOTAL AMOUNT DUE
RECEIVED (Current Billing Cycle)
_ C1,72,729.00
TOTAL CREDIT LIMIT
(Including Cash) AVAILABLE CREDIT LIMIT AVAILABLE CASH LIMIT MINIMUM DUE DUE DATE
C8,640.00 21 May, 2026
C3,98,000 C2,25,271 C1,59,200
Past Dues OVER LIMIT 3 MONTHS + 2 MONTHS 1 MONTH CURRENT DUES MINIMUM DUES
(if any) C0.00 C0.00 C0.00 C0.00 C8,640.00 C8,640.00
l If the minimum amount due or part amount less than the total amount due is paid, interest charges are applicable.
Insurance provider: HDFC ERGO. Toll Free: +800 8250825/1204507250 (Chargeable)
"""

# YES Bank Klick puts dues on the line below the label row (live July-2026 layout).
YESBANK_KLICK = """
Credit Card Statement
YES BANK KLICK
Statement for YES BANK Card Number 3561XXXXXXXX2653
Previous Balance :
Rs. 6,062.00 Dr
Statement Period: Credit Limit:
13/06/2026 To 12/07/2026 Rs. 4,00,000.00 Current Purchases / Cash Advance
Available Credit Limit: & Other Charges :
Statement Date : 12/07/2026 Rs. 3,87,745.00 Rs. 16,455.00 Dr
Total Amount Due: Cash Limit: Points Earned : 0
Rs. 12,255.00 Rs. 0.00
Payment & Credits Received :
Minimum Amount Due: Available Cash Limit: Rs. 10,262.00 Cr
Rs. 245.10 Rs. 0.00
Payment Due Date: 01/08/2026 YES ONLINE Other Mode

Transaction Details
13/06/2026 UPI_SWIGGY BANGALORE Ref No: RT123 420.00
14/06/2026 AMAZON PAY INDIA 1,250.00
20/06/2026 ZOMATO ONLINE 380.50
01/07/2026 PAYMENT RECEIVED THANK YOU 6,062.00 Cr
05/07/2026 FLIPKART INTERNET 890.00
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

# Real Flipkart Axis layout from pdfplumber: a PAYMENT SUMMARY header row, then
# values with Dr/Cr. The reconciliation formula also contains "=Total Payment
# Due" above the previous-balance line — that must not steal the total.
AXIS_PAYMENT_SUMMARY = """
Flipkart Axis Bank VISA Credit Card Statement
ADA LOVELACE
PAYMENT SUMMARY
Total Payment Due Minimum Payment Due Statement Period Payment Due Date Statement Generation Date
554.00 Dr 100.00 Dr 17/09/2025 - 15/10/2025 04/11/2025 15/10/2025
Credit Card Number Credit Limit Available Credit Limit Available Cash Limit
For hassle free payments register for
440006******0406 402,000.00 401,446.00 120,600.00 Auto-Debit facility on 18605005555
Previous Balance - Payments - Credits + Purchase + Cash Advance + Other Debit&Charges =Total Payment Due Making only the minimum payment every
month would result in the repayment stretching
48.00 Cr 0.00 418.00 1,020.00 0.00 0.00 554.00 Dr over years with consequent interest payment
Account Summary
Card No: 440006******0406 Name ADA LOVELACE
25/09/2025 FLIPKART PAYMENTS,BANGALORE MISC STORE 225.00 Dr
**** End of Statement ****
Minimum Amount Due (MAD) by the due
Interest on Rs. 10,000 @ 3.75% p.m. from 11th July to 20th July
Total Amount Due 8813.65
Minimum Amount Due 1953.65
"""

# Ace card: larger total; same header shape; last4 0478.
AXIS_ACE_PAYMENT_SUMMARY = """
Axis Bank ACE Credit Card Statement
PAYMENT SUMMARY
Total Payment Due Minimum Payment Due Statement Period Payment Due Date Statement Generation Date
16,009.00 Dr 321.00 Dr 17/09/2025 - 15/10/2025 04/11/2025 15/10/2025
Credit Card Number Credit Limit Available Credit Limit Available Cash Limit
For hassle free payments register for
470011******0478 402,000.00 381,861.04 120,600.00 Auto-Debit facility on 18605005555
Previous Balance - Payments - Credits + Purchase + Cash Advance + Other Debit&Charges =Total Payment Due Making only the minimum payment every
month would result in the repayment stretching
52.00 Cr 0.00 0.00 16,061.00 0.00 0.00 16,009.00 Dr over years with consequent interest payment
Card No: 470011******0478 Name ADA LOVELACE
**** End of Statement ****
"""

# Credit balance statements print Cr on the payment summary amounts.
AXIS_CREDIT_BALANCE = """
Airtel Axis Bank Mastercard Credit Card Statement
PAYMENT SUMMARY
Total Payment Due Minimum Payment Due Statement Period Payment Due Date Statement Generation Date
154.00 Cr 0.00 Cr 14/04/2026 - 12/05/2026 01/06/2026 12/05/2026
Credit Card Number Credit Limit Available Credit Limit Available Cash Limit
For hassle free payments register for
539494******0083 402,000.00 402,000.00 120,600.00 Auto-Debit facility on 18605005555
Previous Balance - Payments - Credits + Purchase + Cash Advance + Other Debit&Charges =Total Payment Due Making only the minimum payment every
month would result in the repayment stretching
310.00 Dr 0.00 0.00 0.00 0.00 0.00 154.00 Cr over years with consequent interest payment
Card No: 539494******0083 Name ADA LOVELACE
**** End of Statement ****
"""

AMEX_INLINE = """
American Express
Prepared for ADA LOVELACE
Account Number XXXX XXXXXX 91004

Closing Date July 20, 2026
New Balance $ 0.00
New Balance 1,84,220.75
Minimum Payment Due 9,211.00
Payment Due Date August 8, 2026
Total Credit Limit 12,00,000.00
"""

HSBC_INLINE = """
HSBC Credit Card Statement
HSBC LIVE+ CREDIT CARD
Card Number 5120 XXXX XXXX 4200
Statement Date 22/07/2026
Payment Due Date 11/08/2026
Total Dues 18,450.00
Minimum Amount Due 920.00
Credit Limit 2,50,000.00
Available Credit 2,31,550.00
"""

# Live Jul-2026 layout: Net Outstanding must not beat Total Payment Due;
# Minimal payment due; period end is the bill date; 43xx…7672 last4.
# Transaction lines use compact DDMMM dates (25JUN) with optional CR.
HSBC_LIVE_JULY = """
HSBC LIVE+ CREDIT CARD Statement
MR ADA LOVELACE
43xx xxxx xxxx 7672

PAYMENT SUMMARY
Payment due date Minimal payment due ( )
06 AUG 2026 202.53

XXXXXXXXXXX Statement period Total payment due ( )
XXXXXXXXXXX 23 JUN 2026 To 22 JUL 2026 20,252.96

Credit limit ( ) Cash limit ( )*
100,000.00 20,000.00
Available Credit Limit 79,747.04

DATE TRANSACTION DETAILS AMOUNTS ( )
OPENING BALANCE 17,367.28
30JUN BBPS PMT BBPSDP016181185431LHnWAo 17,367.28 CR
PURCHASES & INSTALLMENTS
23JUN CASHBACK CREDIT 1,110.00 CR
25JUN MW KPN FF 3072 WHITEFIELD BANGALORE 594.92
25JUN Zepto Marketplace Priv Bangalore IN 2,858.00
02JUL MW THE PIZZA BAKERY BANGALORE 956.00
22JUL NET OUTSTANDING BALANCE 20,252.96

ACCOUNT SUMMARY
Opening balance ( ) Purchase & other charges ( ) Payment & other credits ( ) Net Outstanding balance ( )
17,367.28 21,362.96 18,477.28 20,252.96
"""

HSBC_MITC = """
Most Important Terms and Conditions
HSBC Credit Card
Credit Limit 5
Payment Due Date 13 Dec 1300
Total Amount Due 21,500.00
Minimum Amount Due 1,000.00
"""

# Swiggy HDFC: Credit Card No. is 6527; alternate account digits must not become 2476.
HDFC_SWIGGY_ALTERNATE = """
Swiggy HDFC Bank Credit Card Statement
Ada Lovelace Credit Card No. 526873XXXXXX6527
RA Alternate Account Number 0001010610012476521
Statement Date 20 Feb, 2026
Total Amount Due 4,551.00
Minimum Due 0.00
"""

# Savings-account e-statement: marketing mentions credit cards; must not become a card.
ICICI_SAVINGS_ESTATEMENT = """
STATEMENT SUMMARY for Customer ID: XXXXX0018 in INR as on June 30, 2026.
Savings A/c XXXXXXXX5705 35,893.68 Registered
Statement of Transactions in Savings Account XXXXXXXX5705 in INR for the period June 01, 2026 - June 30, 2026
on ICICI Bank Credit Cards, Debit Cards
& Net Banking.
Closing Balance 208.00
DEPOSITORY
LTD-0001IN30302868125795-00012050001396-HDFC00
08-06-2026 UPI payment 122.23
"""

# SBI PhonePe / Cashback often print only the last two digits.
SBICARD_PARTIAL_TAIL = """
SBI Card Statement
XXXX XXXX XXXX XX18
Statement Date: 24 Jul 2026
Total Amount Due Minimum Amount Due Payment Due Date
INR 4,512.00 INR 225.00 13 Aug 2026
Credit Limit: INR 1,50,000.00
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
