# ফিল্ড ও পাইপলাইন (Fields & pipelines)

রেকর্ডে নিজের ফিল্ড যোগ করুন এবং বিক্রির ধাপ সাজান।

## কাস্টম ফিল্ড (Custom fields)

person, company ও deal-এর জন্য বাড়তি ফিল্ড।

1. ফিল্ডটি কিসের জন্য তা বেছে নিন (people, companies বা deals)।
2. একটি **Label** ও **Type** দিন: text, long text, number, date, dropdown, multiple choice, yes/no বা link।
3. dropdown ও multiple choice-এর জন্য **Options** দিন, প্রতি লাইনে একটি।
4. সংরক্ষণ করুন। ফিল্ডটি ফর্মে, export-এ, API-তে এবং merge field হিসেবে দেখা যায়।

উদাহরণ: person-এ "Booking engine" (dropdown), company-তে "Rooms" (number)।

## পাইপলাইন (Pipelines)

পাইপলাইন হলো ধাপের তালিকা, যার মধ্য দিয়ে একটি ডিল এগোয়।

* ডিফল্ট হলো New lead → Contacted → Qualified → Proposal sent → Negotiation → Won / Lost।
* ধাপের নাম, তাদের **জেতার সম্ভাবনা (win probability)** এবং ক্রম বদলাতে পারেন।
* ভিন্ন ধরনের জিনিস বিক্রি করলে আরেকটি পাইপলাইন যোগ করুন।

জেতার সম্ভাবনা [Deals](/help/deals) ও [Dashboard](/help/dashboard)-এর weighted পূর্বাভাস নির্ধারণ করে।

## জেনে রাখুন

শুধু **owner ও admin** ফিল্ড ও পাইপলাইন বদলাতে পারেন।
