---
subject: Confirm your PocketShell email
name: PocketShell signup confirmation
category: transactional
required_context:
  - name: confirm_url
    description: Link that verifies this address
example_context:
  confirm_url: https://pocketshell.io/?token=example
---

Confirm your email to hear from PocketShell.

[Verify your email]({{ confirm_url }})

If you did not ask for this, you can ignore this message. The link expires in 48 hours.
