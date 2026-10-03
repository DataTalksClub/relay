---
subject: Confirm your email
name: Agent Git Lab signup confirmation
category: transactional
required_context:
  - name: confirm_url
    description: Link that verifies this address
example_context:
  confirm_url: https://alexeygrigorev.com/cloudflare-agent-git/subscribe/?token=example
---

Confirm your email to hear about the agent git lab.

[Verify your email]({{ confirm_url }})

If you did not ask for this, you can ignore this message. The link expires in 48 hours.
