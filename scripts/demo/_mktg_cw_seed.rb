# Marketing seed for the Chatwoot agent inbox (account 1, "Polaris HQ").
# Idempotent: conversations + contacts tagged additional_attributes['mktg']=true,
# deleted on re-run. Clean F&B/SME content, fake contacts. NO real customer data.
acc = Account.find(1)
wa = acc.inboxes.find_by(name: 'Polaris HQ — WhatsApp')
ig = acc.inboxes.find_by(name: 'Polaris HQ — Instagram')
me = acc.inboxes.find_by(name: 'Polaris HQ — Messenger')
agent = acc.users.find_by(email: 'agent-polaris@e2e.polaris-local.dev')

# --- cleanup prior seed (resilient: skip callback failures, fall back to delete) ---
conv_ids = Conversation.where(account_id: 1).where("additional_attributes->>'mktg' = 'true'").pluck(:id)
Message.where(conversation_id: conv_ids).delete_all
Conversation.where(id: conv_ids).to_a.each do |c|
  begin; c.destroy; rescue StandardError; c.delete; end
end
Contact.where(account_id: 1).where("additional_attributes->>'mktg' = 'true'").to_a.each do |c|
  begin; c.destroy; rescue StandardError; c.delete; end
end

rows = [
  { name: 'Aisyah Rahman', phone: '+60128840012', inbox: wa, label: 'bot-handling', assign: false, turns: [
    [:in,  "Hi, are you open today?"],
    [:out, "Hi Aisyah! Yes, we're open today 9am to 6pm 😊 Would you like me to book you an appointment?"],
    [:in,  "maybe later, thanks!"],
  ] },
  { name: 'Wei Jie Lim', phone: '+60173320988', inbox: wa, label: 'bot-handling', assign: false, turns: [
    [:in,  "boleh share your pricing?"],
    [:out, "Of course! The first consultation is free (30 min), and a full session is RM250 (1 hour). Would you like me to book you a slot?"],
    [:in,  "yes please"],
    [:out, "Great! 🙂 I have openings this Thursday and Friday afternoon. Which works better for you?"],
  ] },
  { name: 'Daniel Tan', phone: '+60192245571', inbox: wa, label: 'bot-handling', assign: false, turns: [
    [:in,  "I'd like to book a consultation this Saturday 3pm"],
    [:out, "Got it Daniel, a 30-min Consultation this Saturday (7 Jun) at 3:00pm. Shall I confirm the booking?"],
    [:in,  "yes confirm please"],
    [:out, "✅ Booked! Consultation, Sat 7 Jun, 3:00pm under Daniel. You'll get a reminder 2 hours before. See you then!"],
  ] },
  { name: 'Nurul Huda', phone: '+60134456120', inbox: ig, label: 'bot-handling', assign: false, turns: [
    [:in,  "do you offer online sessions?"],
    [:out, "Yes! 💻 We offer both online and in-person sessions. Want me to book you one?"],
  ] },
  { name: 'Kavin Raj', phone: '+60165539071', inbox: me, label: 'human-handling', assign: true, turns: [
    [:in,  "Hi, I'd like to arrange sessions for my whole team, can someone help?"],
    [:out, "Of course 🙏 For team bookings I'll connect you with our team. Someone will reach out shortly to sort out the details."],
    [:in,  "okay thank you"],
  ] },
  { name: 'Siti Mariam', phone: '+60198812204', inbox: wa, label: 'bot-handling', assign: false, turns: [
    [:in,  "any promo this month?"],
    [:out, "Yes! 🎉 This month: a free 30-min consultation with every full session booked. Want me to book you in?"],
    [:in,  "ok book for tomorrow"],
    [:out, "You got it 😊 Session booked for tomorrow, with your free consultation included. See you then, Siti!"],
  ] },
  { name: 'Farah Idris', phone: '+60134567720', inbox: wa, label: 'bot-handling', assign: false, turns: [
    [:in,  "where are you located?"],
    [:out, "We're at Level 12, Menara Maxis, KLCC 📍 We also offer online sessions. Anything else I can help you with?"],
  ] },
]

now = Time.current
rows.each_with_index do |r, idx|
  contact = Contact.create!(account: acc, name: r[:name], phone_number: r[:phone],
                            additional_attributes: { 'mktg' => true })
  ci = ContactInbox.create!(contact: contact, inbox: r[:inbox], source_id: SecureRandom.uuid)
  conv = Conversation.create!(account: acc, inbox: r[:inbox], contact: contact,
                              contact_inbox: ci, status: :open,
                              additional_attributes: { 'mktg' => true })
  base = now - ((rows.length - idx) * 37).minutes
  r[:turns].each_with_index do |(dir, text), ti|
    m = conv.messages.create!(account: acc, inbox: r[:inbox],
                              message_type: (dir == :in ? :incoming : :outgoing),
                              content: text,
                              sender: (dir == :in ? contact : (r[:assign] ? agent : nil)))
    m.update_columns(created_at: base + (ti * 2).minutes, updated_at: base + (ti * 2).minutes)
  end
  conv.add_labels([r[:label]]) if r[:label]
  conv.update!(assignee: agent) if r[:assign] && agent
  conv.update_columns(last_activity_at: base + 10.minutes, created_at: base)
end

puts "seeded #{rows.length} Chatwoot conversations into account #{acc.id}"
puts "open now: #{acc.conversations.open.where("additional_attributes->>'mktg' = 'true'").count}"
