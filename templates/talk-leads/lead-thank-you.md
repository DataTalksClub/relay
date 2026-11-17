---
subject: "Thank you for attending {{ event_title }}"
name: Talk and workshop thank-you
category: transactional
required_context:
  - name: event_title
    description: Title of the talk or workshop
  - name: download_url
    description: Signed link to the guide for this confirmed signup
example_context:
  event_title: Full-Stack Vibe Coding
  download_url: https://materials.dtcdev.click/example/download?t=example
  name: Ada
---

Hi{% if name %} {{ name }}{% endif %},

Thank you for attending {{ event_title }}!

[Here's the guide: From Idea to Production — the 28-prompts working kit (PDF)]({{ download_url }}).

Within a few days I'll send you all the materials I used today.

Let's stay in touch: [send me a LinkedIn connection request](https://www.linkedin.com/in/agrigorev/) and add a note saying where we met.

And [subscribe to my Substack](https://aishippingblog.com/), where I regularly write about agentic engineering.

Alexey Grigorev
