# ইন্টিগ্রেশন (Integrations)

প্রতিটি সংযোগের অবস্থা: ইমেইল, WhatsApp, Claude ও REST API।

## কার্ডগুলো

* **Email (SMTP + IMAP)**: Connected বা Not configured দেখায়। সার্ভার সেটিংস `.env` ফাইল থেকে আসে; [Sending settings](/help/settings)-এ পরীক্ষা করুন।
* **WhatsApp Business**: Connected বা Not connected দেখায়। কার্ডে **How to connect**, Meta-তে বসানোর webhook URL এবং টেস্ট পাঠানোর সুবিধা আছে। একজন admin `.env`-এ চারটি মান বসান।
* **Claude (MCP server)**: কতগুলো API key সক্রিয় এবং কীভাবে সংযুক্ত হবেন। দেখুন [API keys & Claude](/help/api-keys)।
* **REST API**: base ঠিকানা এবং key পেজের লিংক।

## কার্ডগুলোর নিচে

* **Channel accounts**: প্রতিটি সংযুক্ত চ্যানেলের জন্য একটি সারি।
* **Recent webhook deliveries**: WhatsApp থেকে পাওয়া সাম্প্রতিক মেসেজ ও স্ট্যাটাস আপডেট, ত্রুটিসহ। WhatsApp মেসেজ প্রত্যাখ্যাত হলে বা না এলে এখানে দেখুন।

## জেনে রাখুন

WhatsApp-এ সাধারণ লেখা শুধু ব্যক্তির শেষ মেসেজের ২৪ ঘণ্টার মধ্যে চলে; এরপর অনুমোদিত template লাগে ([Templates](/help/templates), [Inbox](/help/inbox))।
