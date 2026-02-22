# RSS Newpspaper Instructions

## Origins

This project initially created with the following prompt:

```
Let's create a local claude sdk agent who has a workflow of taking the supplied data/feed*.opml files, and diligently working through each and every subscription to fetch the feed data, create a fully-populated json file for articles from the last 3 days only, and where it's missing information, do some web searches to find those missing bits, with a goal of creating enough data in the json for a web page to show a "card" for the article - i.e. a title image, the article's canonical link, the article contents and metadata, etc. Stored in data/articles/feed-name/yyyy-mm-dd_HHmmss-feedname-articletitle.json. Re-runs are expected daily, so it must not create duplicate articles in those json files and directories. The agent's workflow must be clearly worked out so that it is broken down into disciplined steps.

Agent SDK documentation starts here: https://platform.claude.com/docs/en/agent-sdk/overview#python

We'll call this agentic_fetcher.py.

Create all the tools the agents in the workflow will need to do the job well. Including, if possible, the BaseModel -derived result class so that all the JSON data files are following the exact same schema. Use annotations on tool parameters to help the agents. Use Field() descriptions on BaseModel -inherited classes' fields to help the agents. All tool parameters MUST be non-optional (no defaults for tool function parameters). Use docstrings on tool functions to help the agents.

We will be using LM Studio locally, and example.env has the settings, and has already been copied as-is to .env.
```

# Development Instructions

Remember to activate the .venv.
