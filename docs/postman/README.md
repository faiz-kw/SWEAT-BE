# SWEAT Member Mobile App API Suite

This folder contains the official **Postman Collection** and **Environment** files designed strictly for **SWEAT Mobile Application Developers** (iOS, Android, React Native, Flutter).

---

## 📁 Files Included

1. **`SWEAT_Member_App_Collection.json`**  
   Clean, 100% member-centric Postman collection containing only the user-facing app flows.
2. **`SWEAT_Member_App_Environment.postman_environment.json`**  
   Pre-configured environment variables (`base_url`, `tenant_slug`, `access_token`, `branch_id`, etc.).

---

## 🚀 60-Second Developer Setup

1. Open **Postman** (or Insomnia / Thunder Client).
2. Click **Import** (top left).
3. Drag & drop both files:
   - `SWEAT_Member_App_Collection.json`
   - `SWEAT_Member_App_Environment.postman_environment.json`
4. Select the environment in Postman's top-right dropdown: **`SWEAT Member Mobile App Environment`**.

---

## ⚡ Zero-Effort Authentication

The app developer does **NOT** have to copy-paste JWT Bearer tokens:

1. Open folder **`01. Onboarding & Authentication`**.
2. Run **`1.1 Request Login OTP`** with your test mobile number.
3. Run **`1.2 Verify OTP & Auto-Login`** with the 6-digit OTP (in DEBUG mode, `123456` works).
4. The Postman test script **automatically extracts the JWT `access_token` and saves it to your active environment**.
5. All subsequent requests in the collection (`/mobile/me/`, `/bookings/book/`, `/checkout/`, etc.) will automatically use this token!

---

## 📱 Mobile App Screen-by-Screen Mapping

| Collection Folder | Mobile App Screen / Feature |
| :--- | :--- |
| **01. Onboarding & Authentication** | Phone OTP login screen, Profile Setup, and PAR-Q Medical Clearance Questionnaire. |
| **02. Studio Discovery & Schedule** | Explore studio branches (or find nearest via GPS), browse workout types (Pilates, Bootcamp, PT), and view live daily class schedule timetable. |
| **03. Booking & Attending Classes** | Claim 1st free trial session, book class using package credit, view upcoming/past bookings, reschedule, cancel, and **turnstile check-in dynamic QR pass**. |
| **04. Memberships, Packs & Checkout** | View active session credit balances, browse sellable packages (72, 144, 365 sessions), validate promo code (`BOOTCAMP25`), Razorpay checkout sheet order creation & signature verification, and view tax invoices. |
| **05. Membership Self-Service** | Request membership freeze (pause for travel/medical) and resume early (unfreeze). |

---

## 🔑 Key Headers

| Header | Description |
| :--- | :--- |
| `Authorization` | `Bearer {{access_token}}` (auto-populated after OTP verify) |
| `X-Tenant-Slug` | `sweat` |
| `Content-Type` | `application/json` |
