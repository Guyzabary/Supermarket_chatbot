import pandas as pd
import re
from rapidfuzz import fuzz

# 1. קריאת הקובץ ואיחוד רק 4 סופרים
xls = pd.ExcelFile("סופרים.xlsx")
target_supers = ['רמי לוי', 'שופרסל', 'יוחננוף', 'קרפור-מגה בעיר']
all_data = []

for sheet in xls.sheet_names:
    if sheet in target_supers:
        df = xls.parse(sheet)
        df['סופר'] = sheet
        all_data.append(df)

df = pd.concat(all_data, ignore_index=True)

# 2. ניקוי שם המוצר
def clean_name(name):
    if pd.isnull(name):
        return ""
    name = str(name).lower()
    name = re.sub(r'[^\w\s]', '', name)             # סימני פיסוק
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'\b\d+%?\b', '', name)           # אחוזים
    name = re.sub(r'\b\d+[גקמל]?\b', '', name)      # יחידות
    return name

df["CleanItemName"] = df["ItemName"].apply(clean_name)

# 3. השוואת שמות – לפי כל זוג סופר
grouped = df.groupby('סופר')

# רשימה של כל המוצרים בכל סופר
products_by_super = {super_name: group[['CleanItemName', 'ItemName']].drop_duplicates()
                     for super_name, group in grouped}

# 4. חיפוש מוצרים משותפים בכל 4 הסופרים (לפי דמיון טקסטואלי >= 85)
matched_products = []

for index_rl, row_rl in products_by_super['רמי לוי'].iterrows():
    name_rl = row_rl['CleanItemName']
    count_match = 1
    row_match = {'רמי לוי': row_rl['ItemName']}
    found = True

    for other_super in ['שופרסל', 'יוחננוף', 'קרפור-מגה בעיר']:
        best_score = 0
        best_match = None
        for index_other, row_other in products_by_super[other_super].iterrows():
            score = fuzz.token_sort_ratio(name_rl, row_other['CleanItemName'])
            if score > best_score:
                best_score = score
                best_match = row_other['ItemName']
        if best_score >= 85:
            row_match[other_super] = best_match
            count_match += 1
        else:
            found = False
            break

    if found and count_match == 4:
        row_match['שם נקי'] = name_rl
        matched_products.append(row_match)

# 5. הפקה לקובץ של 300 מוצרים בלבד
final_df = pd.DataFrame(matched_products).drop_duplicates().head(300)
final_df.to_excel("300_מוצרים_זהים_בכל_הסופרים.xlsx", index=False)

print("נמצאו", len(final_df), "מוצרים תואמים בכל 4 הסופרים.")
print(final_df.head(10))
