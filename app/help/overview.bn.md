# শুরু করুন: পুরো সিস্টেমের ধারণা

সিআরএম কীভাবে কাজ করে এবং কোন অংশ কোথায় পাবেন।

## সিস্টেমটি কীভাবে কাজ করে

এই সিআরএম **মানুষ (People)** কেন্দ্র করে তৈরি। বাকি সবকিছু একজন মানুষের সঙ্গে যুক্ত থাকে:

```
Company ── People ──┬── ইমেইল / ফোন / WhatsApp নম্বর      (যোগাযোগের মাধ্যম)
                    ├── Timeline: নোট, কল, মেসেজ, ক্যাম্পেইনের ইমেইল
                    ├── Tasks                              (পরবর্তী কাজ)
                    ├── Deals                              (সম্ভাব্য বিক্রি)
                    ├── Inbox-এর কথোপকথন                   (ইমেইল + WhatsApp)
                    └── ক্যাম্পেইনে Enrollment             (স্বয়ংক্রিয় ইমেইল সিকোয়েন্স)
```

## বাম পাশের মেনু

| মেনু | কী কাজে লাগে | গাইড |
|---|---|---|
| **Dashboard** | পাইপলাইনের মূল্য, আজকের টাস্ক, অপঠিত মেসেজ, ক্যাম্পেইনের পরিসংখ্যান | [ড্যাশবোর্ড](/help/dashboard) |
| **Inbox** | সব রিপ্লাই ও WhatsApp চ্যাট, প্রতি ব্যক্তির জন্য একটি থ্রেড | [ইনবক্স](/help/inbox) |
| **Tasks** | আপনার ফলো-আপ | [টাস্ক](/help/tasks) |
| **People / Companies** | কন্টাক্ট ও প্রতিষ্ঠান | [People](/help/people), [Companies](/help/companies) |
| **Deals** | বিক্রির পাইপলাইন | [Deals](/help/deals) |
| **Campaigns, Enrollments, Outbox, Email activity** | স্বয়ংক্রিয় ইমেইল সিকোয়েন্স এবং তার অবস্থা | [Campaigns](/help/campaigns), [Enrollments](/help/enrollments), [Outbox](/help/outbox), [Email activity](/help/activity) |
| **Import** | CSV / Excel তালিকা আনা | [Import](/help/import) |
| **Templates, Tags, Do not contact** | পুনর্ব্যবহারযোগ্য লেখা, লেবেল, ব্লক তালিকা | [Templates](/help/templates), [Tags](/help/tags), [Do not contact](/help/suppression) |
| **Workspace** | সেটিংস, ইন্টিগ্রেশন, ফিল্ড, ইউজার, API key, অডিট লগ | [Sending settings](/help/settings), [Integrations](/help/integrations), [Fields & pipelines](/help/fields), [Users](/help/users), [API keys & Claude](/help/api-keys), [Audit log](/help/audit) |

## তিনটি নিয়ম যা সব জায়গায় প্রযোজ্য

1. **যেকোনো মাধ্যমে রিপ্লাই এলে স্বয়ংক্রিয় ইমেইল বন্ধ হয়ে যায়** (ইমেইল রিপ্লাই বা WhatsApp মেসেজ)।
2. **অপ্ট-আউট সর্বজনীন।** আনসাবস্ক্রাইব বা "do not contact" হলে সব ক্যাম্পেইন ও সব মাধ্যম বন্ধ হয়।
3. **সবকিছু লগ হয়।** আপনি, সহকর্মী, API বা Claude যা-ই পরিবর্তন করুক, তা [অডিট লগে](/help/audit) থাকে।

## প্রথমবার সেটআপ (প্রায় ১৫ মিনিট)

1. সার্ভারের `.env` ফাইলের অ্যাডমিন লগইন দিয়ে **সাইন ইন** করুন।
2. *Workspace > Users & API keys*-এ গিয়ে **নিজের ইউজার তৈরি করুন**। প্রথম ইউজারই owner হন।
3. *Sending settings*-এ গিয়ে **ইমেইল পাঠানো যাচাই করুন**: টেস্ট ইমেইল পাঠান, IMAP টেস্ট চালান, ডিলে, দৈনিক সীমা, সিগনেচার ও আনসাবস্ক্রাইব ফুটার ঠিক করুন।
4. **ঐচ্ছিক:** WhatsApp সংযুক্ত করুন ([Integrations](/help/integrations)), কাস্টম ফিল্ড যোগ করুন ও বিক্রির ধাপ ঠিক করুন ([Fields & pipelines](/help/fields))।

## সাহায্য কোথায় পাবেন

প্রতিটি পেজের নিচে ডান কোণায় একটি **Help** বোতাম আছে, যেখানে **English** ও **বাংলা** লিংক রয়েছে। সেটি আপনি যে পেজে আছেন তার গাইড খোলে। সম্পূর্ণ তালিকা মেনুর *Help & guides*-এ পাবেন।
