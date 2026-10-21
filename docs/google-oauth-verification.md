# Google OAuth verification pack for Vidzy (YouTube)

Why this matters: until the app is verified **and** published ("In production"), only
people you list as test users can connect YouTube, everyone else sees an
"unverified app" warning, and Google expires every YouTube login after **7 days**
(the `invalid_grant` errors in the streams log). Streams can't be offered to the
public until this is done.

What this file covers: what is already fixed in the code and pages, the exact
Google console steps, copy-paste answers for the scope justification, and a
shot-by-shot script for the required demo video.

> Not checked from here: the current status in the console (Testing vs In
> production, which scopes are listed, whether `vidzone.live` is a verified
> domain). Compare the "Console checklist" below against what you see.
> Google changes its console and review rules, so where Google's own
> Verification Center lists something different, follow Google.

---

## 1. Already done in the repo

| Requirement | Status |
|---|---|
| Public home page that explains what the app does | `https://vidzone.live/` - landing page describes real 24/7 YouTube Live streaming |
| Privacy policy on the same domain, linked from the home page | `https://vidzone.live/privacy`, linked in the landing footer |
| Terms of service | `https://vidzone.live/terms` |
| Limited Use statement (Google API Services User Data Policy) | Added to the privacy policy |
| Plain description of what YouTube data is accessed, stored, and why | Added ("How Vidzy uses Google and YouTube user data") |
| YouTube Terms of Service + Google privacy policy links, how to revoke access | Already present |
| Data deletion instructions | `https://vidzone.live/data-deletion` |
| Minimum scopes | Code now requests only `youtube.force-ssl` (the redundant `youtube.readonly` was removed) |

## 2. Console checklist (Google Cloud project `vidzone-app` -> Google Auth Platform)

1. **Search Console: verify the domain.** Go to <https://search.google.com/search-console>,
   add `vidzone.live` as a *Domain* property and add the DNS TXT record it gives you.
   Use the same Google account that owns the Cloud project. Google checks this.
2. **Branding**
   - App name: `Vidzy`
   - User support email and developer contact email: an inbox you read (Google emails the reviewer questions here)
   - App logo: 120x120 PNG (`resource/public/logo-icon.png` can be resized)
   - App home page: `https://vidzone.live/`
   - Privacy policy link: `https://vidzone.live/privacy`
   - Terms of service link: `https://vidzone.live/terms`
   - Authorized domains: `vidzone.live`
3. **Audience**
   - User type: **External**
   - Publishing status: click **Publish app** so it reads **In production**
     (this alone stops the 7-day expiry, but unverified apps still show the warning and are capped at 100 users).
4. **Data access (scopes)** - add exactly one:
   - `https://www.googleapis.com/auth/youtube.force-ssl` ("Manage your YouTube account")
   - Remove `youtube.readonly` and anything else not needed.
5. **Clients** - the Web client used by Vidzy must have this authorized redirect URI:
   - `https://vidzone.live/api/v1/saas/youtube/callback`
     (the app builds this from the **publish_base_url** value in Admin Settings, so that
     setting must be exactly `https://vidzone.live`, no trailing path)
6. **Verification Center** - start verification, paste the justification text from section 3,
   and give the link to the demo video (section 4). Answer the questionnaire honestly.

Expect Google to email questions. Reply promptly; a slow reply restarts the queue.

## 3. Copy-paste: scope justification

**Scope:** `https://www.googleapis.com/auth/youtube.force-ssl`

**How the scope is used (short form):**

> Vidzy lets a creator run a video they already own as a continuous YouTube Live
> stream on their own channel, and publish videos they generated in Vidzy. When the
> user clicks "Go Live", Vidzy uses this scope to create a live broadcast and its
> ingest stream on the user's channel (liveBroadcasts.insert, liveStreams.insert,
> liveBroadcasts.bind), start and end the broadcast (liveBroadcasts.transition), and
> set its cover image (thumbnails.set). It also uploads videos the user asks it to
> publish (videos.insert) and reads the connected channel's name and the live
> viewer and view counts of broadcasts Vidzy created (channels.list, videos.list)
> so they can be shown in the user's dashboard.

**Why a narrower scope is not enough:**

> `youtube.upload` only allows uploading videos; it does not allow creating or
> controlling live broadcasts or live streams, which are Vidzy's core feature.
> `youtube.readonly` cannot create broadcasts. Creating and transitioning live
> broadcasts requires `youtube.force-ssl`, and that single scope also covers the
> read calls above, so it is the only YouTube scope Vidzy requests.

**How data is handled:**

> Vidzy stores the user's OAuth tokens, channel name and ID, and the IDs of the
> broadcasts it created. They are used only to perform the actions the user
> requests, are never sold or used for advertising, and are not shared with third
> parties except as needed to run the service. The user can disconnect YouTube
> inside Vidzy (which deletes the stored tokens) or revoke access in their Google
> account at any time. Vidzy's use and transfer of information received from Google
> APIs adheres to the Google API Services User Data Policy, including the Limited
> Use requirements.

## 4. Demo video script (unlisted YouTube video, English, about 3-4 minutes)

Google needs to see the **real** consent screen and how each requested permission is
used. Record the whole screen at 1080p, with the browser address bar visible the
entire time, and narrate or add captions. Upload as *Unlisted* and paste the link.

Use a Google account that is **not** listed as a test user *after* the app is in
production, so the real flow is shown (before that, a listed test user works).

| # | What to show | What to say |
|---|---|---|
| 1 | Open `https://vidzone.live/` (address bar visible). Scroll the landing page. | "Vidzy streams a creator's video to their own YouTube channel 24/7." |
| 2 | Click the Privacy Policy link in the footer; scroll to "How Vidzy uses Google and YouTube user data" and the Limited Use box. | "This is our privacy policy and how we handle YouTube data." |
| 3 | Log in, open the dashboard, click **YouTube** (Connect). | "I connect my channel." |
| 4 | **Pause on Google's consent screen.** The URL (with `client_id`), the app name "Vidzy" and the requested permission must be readable. Click Allow. | "Google shows the app name and the one permission we request: manage your YouTube account." |
| 5 | Back in Vidzy: the YouTube button now shows the channel name. | "Vidzy reads my channel name to show which channel is connected." |
| 6 | Open **Streams**, create a stream from a video (Import from URL or From my videos). | "I choose the video to loop." |
| 7 | Click **Go Live**. Switch to **YouTube Studio -> Go live** and show the new broadcast with its title and thumbnail. | "Vidzy created this live broadcast and its stream through the YouTube Live API." |
| 8 | Open the public watch page; show it playing live. | "The stream is live on my channel." |
| 9 | Back in Vidzy: the stream card with viewer count and views. | "Vidzy reads viewer and view counts of this broadcast to show them here." |
| 10 | Click **Stop**; show the broadcast ending in Studio. | "Stopping ends the broadcast." |
| 11 | In Vidzy click **Disconnect YouTube**. Then open `https://myaccount.google.com/permissions` and show Vidzy is gone / can be removed. | "The user can disconnect any time, and revoke access in Google." |

Tips: use a channel that is already enabled for live streaming (phone-verified, no
live restriction), do one clean take, and keep every Google screen on screen long
enough to read.

## 5. Questions reviewers commonly ask

- **"Why do you need `force-ssl` and not `youtube.upload`?"** Live broadcasts and live
  streams can't be created or controlled with `youtube.upload`. See section 3.
- **"Do you store or share user data?"** Tokens, channel name/ID and broadcast IDs are
  stored to run the service; none are sold, used for ads, or shared (privacy policy).
- **"How does a user delete their data?"** Disconnect in the app, revoke in Google, or
  email the address on `https://vidzone.live/data-deletion`.
- **"Is the app used by the public?"** Yes, signup is open at `https://vidzone.live/`.

## 6. After approval - what to expect next

- **Quota.** The YouTube Data API gives a new project about 10,000 units a day by
  default. Creating a broadcast and a stream costs 50 units each, an upload costs
  1,600, so uploads run out fast. The app already caps uploads per day
  (`youtube_daily_cap`, default 6). To serve many users you must apply for more
  through Google's **YouTube API Services audit / quota extension** form; plan for
  that once real users arrive.
- **Users still need a channel that can live-stream** (phone verified, no recent
  restriction). The connect error message already says so.
- **Existing connections** made while the app was in Testing may have expired; those
  users reconnect once.
- Keep the app's home page, privacy policy, and what the app actually does consistent.
  Google re-checks, and a mismatch is the most common reason for rejection.
