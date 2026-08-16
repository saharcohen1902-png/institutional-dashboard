# פריסה לאתר חי (GitHub Pages + Actions)

המטרה: אתר ציבורי שמתעדכן לבד בענן, בלי תלות במחשב האישי.

מה שכבר מוכן בתיקייה הזו:
- `.github/workflows/deploy.yml` — מריץ את `update.py` בענן (כל יום + בכל push) ומפרסם ל-Pages.
- `.gitignore` — לא מעלה את בסיס הנתונים הגדול ולא קבצים זמניים.
- הקוד כולו ספריית-תקן של פייתון — אין צורך בהתקנות.

## הצעדים שלך (חד-פעמי, ~5 דקות)

### 1. צור repo חדש וריק ב-GitHub
היכנס ל-https://github.com/new → תן שם (למשל `institutional-dashboard`) →
בחר **Public** → **בלי** README/gitignore/license → Create repository.

### 2. דחוף את התיקייה הזו ל-repo החדש
מהטרמינל, בתוך התיקייה `holdings13f`:

```bash
cd ~/proj/mail/holdings13f
git init -b main
git add .
git commit -m "Institutional 13F + COT dashboard"
git remote add origin https://github.com/<USERNAME>/<REPO>.git
git push -u origin main
```

(החלף `<USERNAME>` ו-`<REPO>` בשלך.)

### 3. הפעל GitHub Pages
ב-repo ב-GitHub: **Settings → Pages → Build and deployment → Source: GitHub Actions**.

זהו. תוך כמה דקות ה-Action ירוץ (אפשר לעקוב בלשונית **Actions**), ובסיום
תופיע הכתובת הציבורית ב-**Settings → Pages** — משהו כמו
`https://<username>.github.io/<repo>/`. את הכתובת הזו אפשר לשלוח לכל אחד.

## אופציונלי — הגדרת כתובת קשר משלך ל-SEC
ה-SEC מבקש כתובת קשר בכל בקשה. כברירת מחדל מוגדרת כתובת קיימת בקוד.
כדי להשתמש בכתובת שלך (ולא לחשוף אחרת ב-repo ציבורי):
**Settings → Secrets and variables → Actions → Variables → New variable**,
בשם `SEC_USER_AGENT` ובערך למשל `Your Name you@example.com`.

## קצב העדכון
- ה-Action רץ **כל יום ~05:23 UTC** ובכל push. אפשר גם להריץ ידנית מלשונית Actions.
- דוח COT (שישי) ודוחות 13F (רבעוני) נלכדים בריצה היומית הקרובה.
- הצופים רואים את העדכון ברענון הדף (הנתונים כבר טריים בענן).

## הערה
הריצה הראשונה בענן בונה הכול מאפס (כולל העשרת שווי שוק) ולכן אורכת ~15 דקות.
ריצות המשך מהירות — בסיס הנתונים נשמר במטמון של Actions בין ריצות.
