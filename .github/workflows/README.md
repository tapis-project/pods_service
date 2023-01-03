## Github Actions
The pods service uses github actions for CI. The process in terms of branches/tags is as follows.

## Process
1. Push to dev starts a build and tests the image.
2. On success of dev build, dev is promoted automatically to staging, both repo and image.
3. Prod is not touched except via a button, when pressed, staging is promoted to prod, both repo and image.

## Logic
Hopefully this methodology makes it so changes are pushed in the correct "order". Users cannot manually edit
the prod or staging branches. This is so that changes are pushed to the wrong branch and hop over CI steps.
