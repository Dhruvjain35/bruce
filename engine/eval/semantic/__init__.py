"""Open-set semantic evaluation.

Deliberately outside `bruce_engine`: these are gold labels and unseen phrasings, and a production
matcher that learned them would make the evaluation measure itself. Nothing in the engine imports
this package, and the Dockerfile does not copy it into the deployed image.
"""
