"""Issue #302 (sent 2026-11-09, Relay campaign 18) re-entered as Relay newsletter blocks."""
MC = "https://mcusercontent.com/0d7822ab98152f5afc118c176/"
SUBJECT = "🛠️ Our new free course starts next week | DataTalks.Club Weekly"
PREVIEW = ("This week: our free generative media course starting next week, Project 2 in AI Dev Tools Zoomcamp, "
           "the Machine Learning Zoomcamp midterm, a Q&A and launch stream, two recordings, and Alexey's post on "
           "dark software factories.")
# The preheader (span.mcnPreviewText) of the #302 Mailchimp archive and of the 8 Nov copy.
PREHEADER = ("Production AI Agents for Generative Media starts November 16. Join the Q&A on November 10 and bring "
             "your questions.")
BLOCKS = [
    ("intro", {"issue_label": "#302 · November 9, 2026", "summary": PREVIEW}),
    ("course_promo", {
        "title": "Production AI Agents for Generative Media 2026 Cohort",
        "image_url": MC + "images/49e229b0-6823-ca6a-0e22-072ff21b88ef.png",
        "image_link": "https://courses.datatalks.club/register/production-ai-agents-generative-media/",
        "body": ("What happens when your agent launches expensive, slow GPU jobs that can fail? Production AI Agents "
                 "for Generative Media starts next week, on November 16.\n\nIn this free course with Nebius, you "
                 "build a Personalized Media Factory:\n\n"
                 "- Run image, video, and audio models as serverless GPU Jobs and Endpoints\n"
                 "- Give an agent GPU models as tools, with evaluation guiding regeneration\n"
                 "- Scale to batches with a cost line per asset\n\n"
                 "The materials are open to everyone. Selected participants also get homework, peer review, and "
                 "the capstone. 728 people have already registered."),
        "cta_text": "Register now",
        "cta_url": "https://courses.datatalks.club/register/production-ai-agents-generative-media/"}),
    ("course_update", {
        "title": "Project 2",
        "image_url": MC + "images/c39d593c-3435-b19e-3a98-d2d110924dc8.png",
        "image_link": "https://github.com/DataTalksClub/ai-dev-tools-zoomcamp",
        "body": ("This is the last week for Project 2, the second attempt at the final project.\n\nSubmissions close "
                 "on November 17, 2026 at 00:00 CET. Check [what to build and how it's scored]"
                 "(https://github.com/DataTalksClub/ai-dev-tools-zoomcamp/tree/main/project) before you submit.\n\n"
                 "After the deadline, you review three projects from other participants. Reviews are due "
                 "November 24, 2026 at 00:00 CET."),
        "cta_text": "Submit your project",
        "cta_url": "https://courses.datatalks.club/ai-dev-tools-2026/project/project2"}),
    ("course_update", {
        "title": "Midterm Project",
        "image_url": MC + "images/f6aee0bd-8307-6d8e-2e90-355ccb46929d.png",
        "image_link": "https://github.com/DataTalksClub/machine-learning-zoomcamp",
        "body": ("This is the second and last week of the midterm project. Submissions are due November 17, 2026, "
                 "at 00:00 CET. Check the [project guidelines]"
                 "(https://github.com/DataTalksClub/machine-learning-zoomcamp/tree/main/projects) before you "
                 "submit.\n\nYou don't need to find reviewers in Slack: after the form closes, the course platform "
                 "assigns you three projects to review. Reviews are mandatory and due November 24, 2026 at 00:00 CET."),
        "cta_text": "Submit your midterm project",
        "cta_url": "https://courses.datatalks.club/ml-zoomcamp-2026/project/midterm"}),
    ("course_update", {
        "title": "Q&A, Launch Stream, and Module 1",
        "image_url": MC + "images/f10f4862-ad1e-cda0-050a-21a55b64c1cf.png",
        "body": ("Still deciding if the course is for you? Join the [Pre-Course Live Q&A](https://luma.com/i4x6x2zy) "
                 "on November 10. Alexey Grigorev, Mikhail Rozhkov, and Marouane Khoukh will explain what you'll "
                 "build, who the course is for, the prerequisites, and how to apply. Bring your questions.\n\n"
                 "On November 16, we start together with the [launch stream](https://luma.com/xey7h50c). Module 1 "
                 "covers consistent media generation on serverless GPUs and runs for two weeks, so if you join a "
                 "bit later, you still have time to catch up. Applications stay open until November 30."),
        "cta_text": "Register for the Q&A",
        "cta_url": "https://luma.com/i4x6x2zy"}),
    ("events_list", {
        "events": "\n".join([
            "Mon, November 9 · 2:30 PM CET · Webinar | Answers you can question: Building a verifiable AI analytics agent | https://luma.com/9y6qc72e | with Alex Litvinov",
            "Tue, November 10 · 5:00 PM CET · Live Q&A | Production AI Agents for Generative Media: Pre-Course Live Q&A | https://luma.com/i4x6x2zy | with Alexey Grigorev, Mikhail Rozhkov, and Marouane Khoukh",
            "Mon, November 16 · 5:00 PM CET · Launch stream | Production AI Agents for Generative Media: Course Launch | https://luma.com/xey7h50c | with Alexey Grigorev",
            "Tue, December 15 · 3:00 PM CET · Podcast | Shipping Reliable LLMs and the Rise of AI Engineering | https://luma.com/nassme8x | with Louis-Francois Bouchard",
        ]),
        "footer": ("[See all upcoming events](https://lu.ma/dtc-events) · [Add our events to your calendar]"
                   "(https://calendar.google.com/calendar/?cid=ZjhxaWRqbnEwamhzY3A4ODA5azFlZ2hzNjBAZ3JvdXAuY2FsZW5kYXIuZ29vZ2xlLmNvbQ)")}),
    ("recording", {
        "title": "Workshop Recording: Build a Media Agent: CPU Agent, GPU Models as Tools",
        "image_url": MC + "video_thumbnails_new/72c72f42f6e44de5e1aedc3f4e03a510.png",
        "image_link": "https://www.youtube.com/watch?v=XzS6MrNfrcA",
        "body": ("On November 3, Alexey Grigorev and Mikhail Rozhkov from Nebius ran a hands-on workshop on media "
                 "agents. We built a small agent that runs on a CPU and calls GPU-hosted image models as tools:\n\n"
                 "- Image generation endpoints as agent tools\n- Building and running the agent with Pydantic AI\n"
                 "- Swapping the image model for Sana\n- Nebius Serverless AI: GPU endpoints, jobs, spot instances, and costs\n\n"
                 "This workshop previews Production AI Agents for Generative Media. If you missed the live session, "
                 "the recording is ready.")}),
    ("alexey_article", {
        "title": "From Alexey on Data: Dark Software Factories",
        "image_url": MC + "images/9f6a746c-9197-c639-4aac-e3b0637ab919.png",
        "image_link": "https://aishippingblog.com/p/dark-software-factories",
        "body": ("Do you still read every line of code your agents write?\n\nIn his new newsletter, Alexey explains "
                 "why he stopped reading agent-generated code and looks at how teams run software factories:\n\n"
                 "- Where the term \"dark software factory\" comes from\n"
                 "- How teams run factories, from Sandstorm to Linear-to-PR\n"
                 "- When agent factories still escalate to humans\n"
                 "- The technical and comprehension debt factories create"),
        "cta_text": "Read here",
        "cta_url": "https://aishippingblog.com/p/dark-software-factories"}),
]
