# Service Desk Platform: User Manual

23 November 2026 · Sarthak. Editable copy: https://claude.ai/artifact/6xSFrj1iD91VCPzzKgEbiy

## Getting started

Everyone signs in at the same address with the email and password an administrator gave them; what you see after that depends on your role. Read Getting started, then the section for your role.

**Signing in**

1. Open the portal and enter your email and password.
2. The first time, you are asked to choose a new password: at least 10 characters, not your email address.
3. After 5 wrong attempts the account locks for 15 minutes. Ask an administrator to reset your password if you forget it.
4. You are signed out after 30 minutes without activity, and after 12 hours in any case.

**The screen**

- **Sidebar (left)**: your menus. *Tickets* (Home, New ticket, My tickets, Notifications) is shown to everyone; *Team* (My work, Team queue, Team board, All tickets) to resolvers and managers; *Admin* to administrators. Numbers beside a menu show how many items wait for you.
- **Top bar**: a project picker when you work in more than one project, the bell with your unread notifications, the **+ New ticket** button, and your account menu (top right) with *My account* and *Sign out*.
- **Ticket page**: title and status at the top, the action buttons under it (less common ones are under *More actions*), the description and the activity timeline on the left, and details and SLA timers on the right.

**Finding your role**

Your role is shown under your name in the account menu. You can hold several roles, for example manager of one team and resolver in another; follow every section that applies.

| You are | Read |
| --- | --- |
| Staff raising IT or internal requests | Requester |
| A client's customer | Product user |
| Working tickets in a team | Resolver |
| Leading a team | Team manager (and Resolver) |
| Responsible for a project | Project admin |
| Running the whole installation | Platform admin |

## Requester (staff)

You raise tickets, follow them and answer the team's questions. You see your own tickets and their public history, never internal notes or SLA timers.

**Raise a ticket**

1. Click **+ New ticket** (top bar) or *New ticket* in the sidebar.
2. Choose the project (if you can raise in more than one) and the **Ticket type**: the type decides which team gets it.
3. Pick a **Priority** if the default does not fit.
4. Write a **Title** (up to 200 characters) and a **Description** with what happened, since when and what you tried.
5. Click **Raise ticket**. You get a ticket number such as IT-14 and an acknowledgement in Notifications.

**Follow your tickets**

- *My tickets* lists everything you raised; the sidebar number counts tickets waiting on you.
- Open a ticket to see its status, who is working on it and the activity timeline.
- The bell shows new notifications: assignment, questions, resolution, rejection.

If the team splits your ticket into parts, a **Child tickets** card lists each part's number, title and status. Your ticket is resolved once all its parts are finished.

**Answer a question**

When the resolver needs information the status becomes **Awaiting requester** and you get a notification. Open the ticket, click **Respond**, type your answer and send it. The ticket goes back to In progress. If you do not answer within 7 days, the system rejects the ticket with reason No reply.

**Withdraw a ticket**

No longer needed? Click **Withdraw**, give a short reason and confirm. You can withdraw while the ticket is New, Assigned, On hold or Awaiting requester.

**Reopen a ticket**

If a resolved ticket is not fixed, click **Reopen** within 7 days and say what is still wrong. It goes back to New and to the team. A ticket rejected as Wrong team or Duplicate cannot be reopened: raise a new one instead. After 7 days a finished ticket is Closed for good.

## Product user (client customer)

You are a customer of one of our clients, for example a shop's buyer or a fleet's driver. You raise and follow your own tickets in one project, through the portal, through the client's own app (API), or both, depending on what the client switched on.

**In the portal**

Sign in as described in Getting started. You see only *Home*, *New ticket*, *My tickets* and *Notifications*. Raising, answering, withdrawing and reopening work exactly as for a requester. You can choose a priority only if the client allows it; otherwise the team sets it.

**Through the client's app (for the client's developers)**

The client's product calls the REST API at /api/v1 on the customer's behalf; the full reference is at /api/docs.

1. A project admin creates an API credential for the project and hands over the key and the secret (shown once).
2. Every call sends three headers: X-Project-Key, X-Project-Secret and X-External-User (the customer's id in the client's system).
3. Create or look up the customer with PUT or GET /product-users/{external_id}.
4. Raise a ticket with POST /tickets (project, type, title, description). Send an Idempotency-Key header so a retried request never creates a second ticket.
5. List and read the customer's tickets with GET /tickets and GET /tickets/{ref}; answer, withdraw or reopen with POST /tickets/{ref}/respond, /withdraw or /reopen.

A product user can never see another customer's tickets, staff names beyond the resolver, internal notes or SLA timers.

## Resolver

You work the tickets of your team. Tickets reach you automatically while you are on shift, by a manager's assignment, or when you claim them from the queue.

**Your day**

- **My work**: the tickets assigned to you, nearest SLA due time first. Start here.
- **Team queue**: new tickets of your teams with nobody assigned. A ticket that found nobody on shift waits here; click **Claim** to take it.
- **Notifications**: new assignments, answers from requesters, escalation warnings.

**Work a ticket**

1. **Start** when you begin; the status becomes In progress and the First response step of the SLA stops.
2. **Request info** when you need something from the requester: type the question. The status becomes Awaiting requester and the SLA clock pauses until they respond.
3. **Hold** when you wait for something else (a part, a supplier, a change window): give the reason, visible only to staff. The SLA pauses. **Resume** when you can continue.
4. **Resolve** when done: write a resolution note the requester will read and, optionally, the actual effort in minutes.

**Other actions** (under *More actions* where shown)

- **Reject** a ticket that is not for your team or not a real request: pick a reason code (Wrong team, Duplicate, Not a ticket, Info missing, Other) and explain. Wrong team and Duplicate cannot be reopened, so the requester raises a new ticket in the right place.
- **Release** gives the ticket back to the queue. Automatic routing skips you once, so it goes to a colleague.

**Split a ticket into child tickets**

When part of the work belongs to another team (for example a new starter who also needs network access), split it off as a child ticket. Team managers and project admins can do this on any open ticket of their scope.

1. On an open ticket assigned to you, find the **Child tickets** card and click **+ Add child ticket**.
2. The *New child ticket* form names the parent. Choose the **Ticket type** of the team that should do this part, keep or change the priority (the parent's by default), write a title and description, and click **Create child ticket**.
3. The child gets its own number, team, resolver and SLA. The parent lists it with its team, resolver and a count such as "0 of 1 finished", and its activity records when a child is created, finished or reopened.

Rules: a child cannot have children of its own. The parent cannot be resolved, rejected or withdrawn while any child is open; those buttons come back once every child is finished. Closing the parent does not close its children.

**Reading the SLA card**

The right-hand card shows each SLA step (for example Assignment, First response, Resolution) with a bar of the working time used, the due time in your team's time zone and the time left. Steps show as running, paused, met or breached; at 80% used the ticket counts as at risk and managers are warned. Times count only during working hours of the SLA calendar.

## Team manager

You keep your team's tickets moving and on time. You can do everything a resolver can on your team's tickets, plus the actions below.

**Watch the team**

- **Team board**: your team's open tickets grouped by status, and the people with how many tickets each holds and whether they are on shift. A list headed *Held by deactivated people* shows tickets that need a new owner.
- **Team queue**: unassigned tickets. Ticket types set to manual assignment always wait here for you.
- **All tickets**: every ticket in your scope, with filters for status, priority, resolver, overdue only, dates and search.
- Notifications warn you when a step reaches 80% of its time and when an SLA is breached.

**Assign and rebalance**

- **Assign** a new ticket: pick the resolver; the list shows who is on shift.
- **Reassign** a ticket to someone else in the same team, with a reason. Tickets cannot move to another team: reject it as Wrong team and ask the requester to raise it again.
- **Release** a ticket back to the queue.

**Priority, estimate and SLA** (under *More actions*)

- **Change priority**: the SLA timers are recalculated for the new priority from the ticket's original start; steps already met stay met.
- **Set estimate**: the expected effort in minutes, compared later with the actual effort.
- **Pause SLA** with a reason when the clock should stop for a reason the status does not show; **Resume SLA** to restart it.
- **Extend SLA**: add minutes to one step's due time, with a reason. Every SLA action is recorded in the ticket history.

## Project admin

You configure one project: who works in it, what can be raised, and how fast it must be handled. An organisation admin does the same for every project of their organisation. On every ticket of the project you can also assign, reassign, release and change priority, estimate and SLA; working a ticket (start, resolve, reject) needs a resolver or team manager role. Configuration is never deleted: switch a record to inactive instead.

**Set up a project** (Admin > Projects > your project)

1. **Teams**: add the teams that will work tickets.
2. **People**: in Admin > Users, open a person and use *Add a role* to make them Resolver or Team manager of a team. For a resolver who works shifts, choose their shift calendar; without one the default working hours apply. To add many people at once use bulk add, which reports the result for each person.
3. **Ticket types** (Admin > Ticket types): name, team and assignment mode. *Automatic* routes to the on-shift resolver with the fewest open tickets; *Manual* waits in the queue for a manager.
4. **SLA policies** (Admin > SLA policies): one per priority, optionally per ticket type (a type policy wins). Add steps with cumulative minutes and the status that stops each, for example Assignment 30 (Assigned), First response 60 (In progress), Resolution 480 (Resolved). *Without a policy* lists priority and type pairs that have none.
5. **Escalation rules** on each step: when (percent of time used, or breached), what (notify team managers, project admins, a person or a team; reassign; raise priority). Each rule fires once per ticket.
6. **Project settings**: reopen window, auto-close delay (must be at least the reopen window), days before an unanswered question is rejected, the routing rule, and for client projects whether customers may use the portal, the API and choose a priority.

**People and access**

- Create accounts in Admin > Users: members belong to an organisation; product users belong to one project and carry the client's product ID. The temporary password is shown once: pass it on safely.
- **Reset password** gives a new temporary password and unlocks the account. **Deactivate** ends the person's sessions at once; their open tickets appear on the team board to reassign.
- **API credentials** for a client's product: create one, copy the secret immediately (it is shown once), revoke it if it leaks.

**Check your work**

The Audit log shows every configuration change in your projects: who, when, and the old and new values.

## Platform admin

You run the installation: the building blocks every project shares, the notification channels and the health of the system. You can also do everything a project admin can, in every project. The first platform admin is admin@example.com with a password set at installation, changed at first sign-in.

**First-time setup, in this order**

1. **Calendars**: working hours per weekday and time zone, holidays, and a 14-day preview of open hours. The default calendar applies wherever no other is chosen; add shift calendars (for example a night shift) for resolvers.
2. **Priorities**: names, order and the one default. **Statuses**: the nine statuses are fixed; you may rename them only.
3. **Organisations**: the internal one and each client. Then **Projects** with a short key (for example IT, SHOP): the key becomes the ticket prefix and is locked after the first ticket.
4. Hand each project to a project admin, or set it up yourself (Project admin section).

**Notifications**

- **Channels**: in-app is always on. Add **Email** with the SMTP host, port, sender address, user name and the name of the environment variable that holds the password (the password itself is never stored), or a **Chat webhook** whose URL sits in an environment variable. Use *Send a test* to check it.
- **Templates**: the subject and body per notification type and channel. Types: acknowledgement, assignment, info request, requester response, rejection, no reply, SLA warning, SLA breach, escalation, resolution. Placeholders: {ticket_number}, {title}, {status}, {priority}, {resolver}, {due_time}, {link}. *Preview with sample data* shows the result.

**Settings** (Admin > Settings)

Global values with their allowed ranges: password length (10), lockout attempts (5) and minutes (15), session idle (30 minutes) and maximum (12 hours), API token lifetime (60 minutes), routing retry (5 minutes), escalation cycle (60 seconds), at-risk threshold (80%), delivery attempts (5). Project values set here are the defaults a project can override. Changes apply at once and are audited.

**Keep it healthy**

- **Dashboard**: overdue, at-risk and oldest unassigned tickets, open counts by status, team and project, and system status.
- **System status** warns when the background worker has not run recently, when notifications failed after all retries, or when jobs pile up. The worker must run all the time: it handles SLA timers, escalations, auto-close, routing retries and email delivery.
- **Audit log**: every configuration change in the installation, filterable.

## Quick reference

Who can take each ticket action. "Own" means only on tickets assigned to you; managers act on any ticket of their team, project admins on any ticket of their project. Platform admins have the project admin's rights everywhere. Product users have the requester's rights on their own tickets.

| Action | Requester | Resolver | Team manager | Project admin |
| --- | --- | --- | --- | --- |
| Raise a ticket | Yes | Yes | Yes | Yes |
| Claim from the queue | | Yes | Yes | |
| Start, hold, resume, request info, resolve | | Own | Yes | |
| Reject | | Yes | Yes | |
| Assign, reassign, release | | Release own | Yes | Yes |
| Change priority, set estimate | | | Yes | Yes |
| Pause, resume, extend SLA | | | Yes | Yes |
| Respond, withdraw, reopen | Own tickets | | | |
| Add a child ticket | | Own | Yes | Yes |

**Troubleshooting**

| Problem | What to do |
| --- | --- |
| Account locked | Wait 15 minutes, or ask an administrator to reset the password, which also unlocks it. |
| Session expired | Sign in again; your page opens where you were. |
| A ticket or page says Not available | You have no access to it, or it does not exist. Ask your manager if you should. |
| Updated meanwhile | Someone changed the ticket at the same time. The page reloads with the latest state and keeps what you typed. |
| Reopen button missing | The 7-day window has passed, or it was rejected as Wrong team or Duplicate. Raise a new ticket. |
| Ticket in the wrong team | Reject it with reason Wrong team; the requester raises it again with the right type. |
| No emails arrive | A platform admin checks Admin > Channels (last failure, Send a test) and the system status page. |
| Automatic ticket not assigned | Nobody was on shift. It waits in the Team queue, is retried every 5 minutes and can be claimed. |
| Resolve, Reject or Withdraw missing, or "Finish its child tickets first" | The ticket has open child tickets, listed in its Child tickets card. Finish them first; the buttons then come back. |
