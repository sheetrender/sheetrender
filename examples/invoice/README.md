# Line-item invoice (grouped mode)

`data.csv` has one row per line item; `invoice_no` identifies the invoice each
line belongs to. Grouped rendering emits one PDF per invoice, with the group's
rows available to the template as `items` and the total computed by
`{{ items | sumcol('line_total') | money2 }}`.

```sh
uvx sheetrender batch template.html data.csv -o out/ \
  --group-by invoice_no --filename "{{ invoice_no }}.pdf" --zip invoices.zip
```

Expected output: `INV-2041.pdf` (3 line items), `INV-2042.pdf` (2),
`INV-2043.pdf` (1), plus `invoices.zip`.
