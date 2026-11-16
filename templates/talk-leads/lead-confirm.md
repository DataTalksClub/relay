---
subject: "Confirm your email to get the materials: {{ event_title }}"
name: Talk lead confirmation (double opt-in)
category: transactional
required_context:
  - name: confirm_url
    description: Link that confirms the address and unlocks the download
  - name: event_title
    description: Title of the talk or workshop page
example_context:
  confirm_url: https://materials.dtcdev.click/example-talk/confirm?t=example
  event_title: Example talk
  offer: The slides and the 28-prompts working kit
  name: Ada
---

Hi{% if name %} {{ name }}{% endif %},

thanks for coming to **{{ event_title }}**!

Please confirm your email address to get the materials{% if offer %}: {{ offer }}{% endif %}.

[Confirm and get the materials]({{ confirm_url }})

The link stays valid, so you can use it again later to download the materials.

If you didn't sign up, ignore this email and you won't hear from me.

Alexey Grigorev
