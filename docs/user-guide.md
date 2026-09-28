# Bike Trip Journal — User Guide

> **Draft** (t-handover-docs-draft). Written from the app as built; finalised once the trip site is live (spec §10). Items marked TODO need the production address.

**Trip address:** TODO — the production URL is not live yet.

## Two kinds of link

You'll be sent one of two links. They look alike (`<address>/t/<long random code>`). What you can do depends on which one you got.

- **Rider link.** For the people on the bikes. You can view everything, add stops and photos, and add or edit bikes.
- **Viewer link.** For friends and family following along. You can see the map, the stops, the photos and the bikes, but you can't change anything.

Treat the rider link like a key. Anyone who has it can add to the journal. If it gets passed around, the organiser can swap it for a new one. Everyone on the trip then needs the new link.

## Opening the trip

Open your link in the phone's browser. The trip name and start date appear at the top.

- **"Waking up the server…"** The site sleeps when nobody is using it, and the first visit can take a little while to start it. Wait a moment.
- **"Can't reach the server"** with a **Retry** button. You're offline, or the site is down, and this phone has never opened the trip before. If it has, the saved trip opens instead.
- **"Trip not found"** The link is wrong or incomplete. Check that you copied the whole link.

## Install it on your home screen (riders: do this)

**iPhone:** open your link in Safari, tap **Share**, then tap **Add to Home Screen**.
**Android:** open the link in Chrome, open the menu, then tap **Install app** or **Add to Home screen**.

The first time you open the installed app, it may show **"Paste your trip link"**. On iPhone, the installed app doesn't share memory with Safari, so it doesn't know your link yet. Paste your link (the whole link, or just the code after `/t/`), then tap **Open trip**. After that, it goes straight to your trip every time.

**Riders: always add stops from the installed app, not from Safari.** Stops waiting to send are stored inside the app you used to add them. If you add a stop in Safari and then switch to the installed app, the installed app can't see that stop. It will only send when you open Safari again.

## Your name (riders only)

The first time you open the rider link, you'll be asked for **Your name**. It's shown on the photos you upload. It's only a label, not a login. It's saved on this phone. If you use a second phone, or both Safari and the installed app, you'll be asked again. Viewers aren't asked for a name.

## Looking at the trip

- **Trip home** shows a map with a pin for each stop and a line joining them in order. Below the map is the list of stops, oldest first.
- Tap a stop to open it. You'll see its name, the time you arrived, notes, and its photos as small thumbnails. Tap a thumbnail to see it full-screen, then tap anywhere to close it.
- **"approximate location"** next to a stop means the rider couldn't get GPS and tapped a spot on the map instead.
- Viewing needs a connection. Offline, the phone keeps only the trip name and the bike list. If you open the app with no signal, the map may show "Map unavailable" and the stop list may fail to load. Adding a stop still works.

## Adding a stop (riders)

1. On trip home, tap **Add stop**.
2. The app tries to get your position from GPS ("Getting GPS fix…"). If GPS is blocked or doesn't work, you'll see **"GPS unavailable: tap the map to set the location"** and a map. Tap where you are. That stop will be marked "approximate location". With no signal, the map may appear blank because its background can't load. Tap as close as you can.
3. Enter a **Name** (required) and any **Notes**.
4. **Photos:** tap the photo button and pick one or more photos, or take new ones. They're shrunk to a smaller size on your phone first ("Processing photos…"). The time each photo was taken is kept. Tap **Remove** next to a photo if you picked the wrong one.
5. Tap **Save stop**. You're taken back to trip home.

The arrival time is set when you open the Add stop screen, not when you tap Save.

Saving never needs a signal. The stop and its photos are saved on your phone first, then sent once there's a connection.

## Offline, and the notices at the top

A notice at the top of every screen shows anything that hasn't been sent yet:

- **"Waiting to send: 2 stops, 5 photos"** These are saved on your phone and will send by themselves. Sending is tried when the app opens, when you switch back to it, and when the phone reconnects. It also keeps retrying in the background, waiting a little longer each time, up to 5 minutes between tries. You don't need to do anything. Don't delete the app or clear its data while items are waiting, or they'll be lost.
- **"<name> still trying: <reason>"** This item has failed to send 10 or more times and is still being retried. It usually means a very weak signal or the site is down. Leave it alone and it will send when it can.
- **"<name> could not be sent: <reason>"** with a **Dismiss** button. The site refused this item, so retrying won't help. It stays on your phone until you tap **Dismiss**. If a stop fails, its photos fail with it ("Not sent: stop … failed"). Before you dismiss, note the details if you want to add the stop again.

## Bikes

Tap **Bikes** on trip home to see each rider's bike: make, model, year and specs. Anyone with either link can see this page.

Riders can also use the **Add bike** form at the bottom, or tap **Edit** on a bike. Rider name, make, model and year are required. Specs are optional.

**Adding and editing bikes needs a connection.** Bike changes aren't queued like stops. If you're offline you'll see "You're offline — try again when connected." What you typed stays in the form, so tap save again once you have a signal.
