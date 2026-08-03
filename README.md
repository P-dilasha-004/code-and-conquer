# StudySync

## Our Team: Code and Conquer

- Saurab Gyawali
- Daniel Reyes
- Dilasha Pant
- Christian Okebe Monembeng

# Deployed App Link
Use this link to run the app: https://study-group-finder-828411740843.us-central1.run.app

# Setup
One person needs to follow SETUP.md to complete setup. Ignore this if it is already done for you!

# Authentication Setup

This project uses Supabase for authentication. Since credentials are not stored in the repository for security reasons, each team member needs to create a local secrets file manually.

**Step 1 — Install the Supabase library:**
```bash
pip install supabase
```

**Step 2 — Create the secrets file:**
```bash
mkdir -p src/.streamlit
nano src/.streamlit/secrets.toml
```

**Step 3 — Add the following to `secrets.toml`:**
```toml
SUPABASE_URL = "https://vovkqyvkafacknjzxhmu.supabase.co"
SUPABASE_KEY = "your-supabase-key-here"
```

Save the file with `Ctrl+X`, `Y`, `Enter`.

**Note:** Do not commit `secrets.toml` to the repository. It is already added to `.gitignore`.

# Distributed Chat Setup (Pub/Sub)

Group chat has two independent paths through Pub/Sub — a durable write
path and a low-latency read path — instead of the app reading/writing
Supabase directly:

```
WRITE (durability):
send_message()  ──publish──▶  Pub/Sub topic  ──push (HTTP)──▶  persist_worker.py
(chat_handler.py)          "chat-messages"                  (separate Cloud Run service)
                            ordering_key=group_id             idempotent upsert into
                                   │                           Supabase `messages`
                                   └──▶ dead-letter topic (after N failures)

READ (low-latency delivery):
                            Pub/Sub topic  ──fan-out sub──▶  chat_subscriber.py
                            "chat-messages"  (1 per app       (in-process, per app instance)
                                              instance)                │
                                                                        ▼
                                              group_chat.py's fragment drains
                                              this in-memory buffer every 0.3s,
                                              with a Postgres reconciliation
                                              check every 10s as a backstop
```

- `src/backend/chat_publisher.py` — publishes to the topic (used by
  `send_message()`)
- `src/worker/persist_worker.py` — a small Flask app that Pub/Sub calls
  directly over HTTP (a push subscription, not a pull loop — fits Cloud
  Run's scale-to-zero model with no idle cost); the only thing that
  actually writes to the `messages` table
- `src/backend/chat_subscriber.py` — a background, in-process subscriber
  (one per running app instance, via a uniquely-named fan-out subscription
  it creates and deletes itself) that buffers incoming messages in memory
  for `group_chat.py`'s fragment to read — this is what makes receiving a
  message push-based instead of poll-based. Degrades gracefully: if no
  Pub/Sub broker is reachable, `get_chat_subscriber()` returns `None` and
  the fragment falls back to Postgres-only polling, same as before this
  file existed.
- `migrations/001_add_client_msg_id.sql` — run this once against the
  Supabase project before deploying the worker; its idempotent upsert
  depends on the `UNIQUE` constraint it adds
- `scripts/setup_pubsub.sh` — provisions the real topic/push
  subscription/DLQ for the write path. **Not run automatically.** The read
  path's fan-out subscription needs no separate provisioning — each app
  instance creates and tears down its own at runtime.

**Streamlit constraint worth knowing**: there's no supported way to force
a *different* browser session's rerun from a background thread, so
`group_chat.py`'s fragment still redraws on a timer (`FAST_POLL_INTERVAL_SECONDS
= 0.3`) rather than a true push-triggered redraw — what changed is that the
timer now reads a cheap in-memory buffer fed by Pub/Sub instead of hitting
Postgres on every tick, so the interval could shrink from 2s to 0.3s
without adding real DB load.

**Important — this changes local dev requirements.** `chat_publisher.py`
and `chat_subscriber.py` both construct Pub/Sub clients, and that
resolves real GCP credentials unless `PUBSUB_EMULATOR_HOST` is set. Since
`app.py` imports the chat pages unconditionally, **the whole app will fail
to start locally** unless you either have real GCP Application Default
Credentials configured, or point at the local emulator:

```bash
export PUBSUB_EMULATOR_HOST=localhost:8085   # needs no emulator actually
export GCP_PROJECT_ID=test-project           # running for the app to start
streamlit run src/app.py --server.port 8080
```

Note: `chat_subscriber.py`'s `create_subscription` call itself needs an
*actually reachable* broker (real GCP or a running emulator) to succeed —
if it isn't reachable, it fails fast (~1s, not the client library's ~60s
default) and `get_chat_subscriber()` returns `None`, which is a supported,
tested fallback (see `tests/chat_subscriber_test.py`), not a crash.

`persist_worker.py` has no such requirement — it's a plain Flask app, run
it directly for local testing:

```bash
SUPABASE_URL=... SUPABASE_KEY=... python3 src/worker/persist_worker.py
# then POST a Pub/Sub-shaped envelope at it — see push_envelope() in
# tests/persist_worker_test.py for the exact shape
```

For automated tests, everything under `tests/chat_*` and
`tests/persist_worker_test.py` mocks the Pub/Sub client / POSTs directly to
Flask's test client — no live Pub/Sub, emulator, or deployed worker needed
to run them. `tests/distributed_chat_integration_test.py` chains the real
producer to the real consumer to verify they agree on the wire format.

# How to Run the Streamlit App
## Step 1: Clone the repository.
In GitHub, go to your team's repository. Click on "Code" then on "SSH" and copy the output.
Open Cloud Shell by going to https://shell.cloud.google.com. **Make sure you are in the correct Google account!**
In the terminal, type `git clone` and then paste what you copied from GitHub. You should see something like this, with your GitHub org and repository name:
```shell
git clone git@github.com:Github-Org-Name/my-team-repository.git
```
Hit enter, and then use `cd` to change into your team's repository.
```shell
cd my-team-repository
```
## Step 2: Run the Streamlit app.
Run this command in the terminal to install the needed packages.
```shell
pip install -r requirements.txt
```
Run the following command to run the app locally. The app entry point is inside the `src` folder.
```shell
streamlit run src/app.py --server.port 8080
```
**Note:** When you make changes, you just need to refresh the webpage and the new changes should appear (*you do NOT need to rerun the previous command while you are actively making changes*).

## Step 3: Using Docker to run the Streamlit app
Docker simply creates a virtual environment for only your app. This is what we will use to deploy our app continuously.
Fortunately, we already have a script that builds and starts Docker for us. Run the following command to build the container and start the server locally.
```shell
./run-streamlit.sh
```
**Note:** This also outputs the same local URL that the previous command output. The only difference is that instead of running the webapp on *your* Cloud Shell, it is running it *within a Docker container*. Don't worry too much about understanding this now, but **if this command fails, your automatic deployment through GitHub Actions will also fail**.

## Step 4: Manual deployment.
First, make sure someone in your team has gone through the steps in SETUP.md. Make sure you have ***Owners*** permission on the GCP project in IAM.
Run the following command to deploy your webapp.
```shell
./manual-deploy.sh
```

# Making Code Changes
After you are assigned a task in the project, how do you actually make the changes and test them out? Follow these steps:
1. Change into your team's repository in Cloud Shell using `cd`.
2. Run `git pull --rebase` to pull in any of your teammates changes.
3. Make changes to the code in Cloud Shell Editor.
4. Run `streamlit run src/app.py --server.port 8080` to see the changes in action (Step 2 above). Don't use CTRL-C and let this command continuously run.
5. Continue to make changes and refresh the web page with the app to see the new changes.
6. When you are ready to be done, use CTRL-C to stop the `streamlit run` command.
7. Check that your changes work in a container by running `./run-streamlit.sh` and make sure you see your changes.
8. Use git to add, commit, and push your changes. It might be good to run `git pull --rebase` before pushing your changes, or optionally use git branches to avoid conflicts with your teammates.
9. In GitHub, once you push to the main branch, check that the Actions succeeded and deployed your changes.
