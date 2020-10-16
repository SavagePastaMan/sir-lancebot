import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Union

import aiohttp
import discord
from discord.ext import commands

from bot.constants import Channels, Month, Tokens, WHITELISTED_CHANNELS
from bot.utils.decorators import in_month, override_in_channel
from bot.utils.persist import make_persistent

log = logging.getLogger(__name__)

CURRENT_YEAR = datetime.now().year  # Used to construct GH API query
PRS_FOR_SHIRT = 4  # Minimum number of PRs before a shirt is awarded
REVIEW_DAYS = 14  # number of days needed after PR can be mature
HACKTOBER_WHITELIST = WHITELISTED_CHANNELS + (Channels.hacktoberfest_2020,)

REQUEST_HEADERS = {"User-Agent": "Python Discord Hacktoberbot"}
if GITHUB_TOKEN := Tokens.github:
    REQUEST_HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"

GITHUB_NONEXISTENT_USER_MESSAGE = (
    "The listed users cannot be searched either because the users do not exist "
    "or you do not have permission to view the users."
)

# using repo topics API during preview period requires an accept header
GITHUB_TOPICS_ACCEPT_HEADER = {"Accept": "application/vnd.github.mercy-preview+json"}


class HacktoberStats(commands.Cog):
    """Hacktoberfest statistics Cog."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.link_json = make_persistent(Path("bot", "resources", "halloween", "github_links.json"))
        self.linked_accounts = self.load_linked_users()

    @in_month(Month.SEPTEMBER, Month.OCTOBER, Month.NOVEMBER)
    @commands.group(name="hacktoberstats", aliases=("hackstats",), invoke_without_command=True)
    @override_in_channel(HACKTOBER_WHITELIST)
    async def hacktoberstats_group(self, ctx: commands.Context, github_username: str = None) -> None:
        """
        Display an embed for a user's Hacktoberfest contributions.

        If invoked without a subcommand or github_username, get the invoking user's stats if they've
        linked their Discord name to GitHub using .stats link. If invoked with a github_username,
        get that user's contributions
        """
        if not github_username:
            author_id, author_mention = self._author_mention_from_context(ctx)

            if str(author_id) in self.linked_accounts.keys():
                github_username = self.linked_accounts[author_id]["github_username"]
                logging.info(f"Getting stats for {author_id} linked GitHub account '{github_username}'")
            else:
                msg = (
                    f"{author_mention}, you have not linked a GitHub account\n\n"
                    f"You can link your GitHub account using:\n```{ctx.prefix}hackstats link github_username```\n"
                    f"Or query GitHub stats directly using:\n```{ctx.prefix}hackstats github_username```"
                )
                await ctx.send(msg)
                return

        await self.get_stats(ctx, github_username)

    @in_month(Month.SEPTEMBER, Month.OCTOBER, Month.NOVEMBER)
    @hacktoberstats_group.command(name="link")
    @override_in_channel(HACKTOBER_WHITELIST)
    async def link_user(self, ctx: commands.Context, github_username: str = None) -> None:
        """
        Link the invoking user's Github github_username to their Discord ID.

        Linked users are stored as a nested dict:
            {
                Discord_ID: {
                    "github_username": str
                    "date_added": datetime
                }
            }
        """
        author_id, author_mention = self._author_mention_from_context(ctx)
        if github_username:
            if str(author_id) in self.linked_accounts.keys():
                old_username = self.linked_accounts[author_id]["github_username"]
                logging.info(f"{author_id} has changed their github link from '{old_username}' to '{github_username}'")
                await ctx.send(f"{author_mention}, your GitHub username has been updated to: '{github_username}'")
            else:
                logging.info(f"{author_id} has added a github link to '{github_username}'")
                await ctx.send(f"{author_mention}, your GitHub username has been added")

            self.linked_accounts[author_id] = {
                "github_username": github_username,
                "date_added": datetime.now()
            }

            self.save_linked_users()
        else:
            logging.info(f"{author_id} tried to link a GitHub account but didn't provide a username")
            await ctx.send(f"{author_mention}, a GitHub username is required to link your account")

    @in_month(Month.SEPTEMBER, Month.OCTOBER, Month.NOVEMBER)
    @hacktoberstats_group.command(name="unlink")
    @override_in_channel(HACKTOBER_WHITELIST)
    async def unlink_user(self, ctx: commands.Context) -> None:
        """Remove the invoking user's account link from the log."""
        author_id, author_mention = self._author_mention_from_context(ctx)

        stored_user = self.linked_accounts.pop(author_id, None)
        if stored_user:
            await ctx.send(f"{author_mention}, your GitHub profile has been unlinked")
            logging.info(f"{author_id} has unlinked their GitHub account")
        else:
            await ctx.send(f"{author_mention}, you do not currently have a linked GitHub account")
            logging.info(f"{author_id} tried to unlink their GitHub account but no account was linked")

        self.save_linked_users()

    def load_linked_users(self) -> dict:
        """
        Load list of linked users from local JSON file.

        Linked users are stored as a nested dict:
            {
                Discord_ID: {
                    "github_username": str
                    "date_added": datetime
                }
            }
        """
        if self.link_json.exists():
            logging.info(f"Loading linked GitHub accounts from '{self.link_json}'")
            with open(self.link_json, 'r', encoding="utf8") as file:
                linked_accounts = json.load(file)

            logging.info(f"Loaded {len(linked_accounts)} linked GitHub accounts from '{self.link_json}'")
            return linked_accounts
        else:
            logging.info(f"Linked account log: '{self.link_json}' does not exist")
            return {}

    def save_linked_users(self) -> None:
        """
        Save list of linked users to local JSON file.

        Linked users are stored as a nested dict:
            {
                Discord_ID: {
                    "github_username": str
                    "date_added": datetime
                }
            }
        """
        logging.info(f"Saving linked_accounts to '{self.link_json}'")
        with open(self.link_json, 'w', encoding="utf8") as file:
            json.dump(self.linked_accounts, file, default=str)
        logging.info(f"linked_accounts saved to '{self.link_json}'")

    async def get_stats(self, ctx: commands.Context, github_username: str) -> None:
        """
        Query GitHub's API for PRs created by a GitHub user during the month of October.

        PRs with an 'invalid' or 'spam' label are ignored

        For PRs created after October 3rd, they have to be in a repository that has a
        'hacktoberfest' topic, unless the PR is labelled 'hacktoberfest-accepted' for it
        to count.

        If a valid github_username is provided, an embed is generated and posted to the channel

        Otherwise, post a helpful error message
        """
        async with ctx.typing():
            prs = await self.get_october_prs(github_username)

            if prs:
                stats_embed = self.build_embed(github_username, prs)
                await ctx.send('Here are some stats!', embed=stats_embed)
            else:
                await ctx.send(f"No valid October GitHub contributions found for '{github_username}'")

    def build_embed(self, github_username: str, prs: List[dict]) -> discord.Embed:
        """Return a stats embed built from github_username's PRs."""
        logging.info(f"Building Hacktoberfest embed for GitHub user: '{github_username}'")
        prs_dict = self._categorize_prs(prs)
        accepted = prs_dict['accepted']
        in_review = prs_dict['in_review']

        n = len(accepted) + len(in_review)  # total number of PRs
        if n >= PRS_FOR_SHIRT:
            shirtstr = f"**{github_username} is eligible for a T-shirt or a tree!**"
        elif n == PRS_FOR_SHIRT - 1:
            shirtstr = f"**{github_username} is 1 PR away from a T-shirt or a tree!**"
        else:
            shirtstr = f"**{github_username} is {PRS_FOR_SHIRT - n} PRs away from a T-shirt or a tree!**"

        stats_embed = discord.Embed(
            title=f"{github_username}'s Hacktoberfest",
            color=discord.Color(0x9c4af7),
            description=(
                f"{github_username} has made {n} valid "
                f"{self._contributionator(n)} in "
                f"October\n\n"
                f"{shirtstr}\n\n"
            )
        )

        stats_embed.set_thumbnail(url=f"https://www.github.com/{github_username}.png")
        stats_embed.set_author(
            name="Hacktoberfest",
            url="https://hacktoberfest.digitalocean.com",
            icon_url="https://avatars1.githubusercontent.com/u/35706162?s=200&v=4"
        )
        stats_embed.add_field(
            name="In Review",
            value=self._build_prs_string(in_review, github_username)
        )
        stats_embed.add_field(
            name="Accepted",
            value=self._build_prs_string(accepted, github_username)
        )

        logging.info(f"Hacktoberfest PR built for GitHub user '{github_username}'")
        return stats_embed

    @staticmethod
    async def get_october_prs(github_username: str) -> Union[List[dict], None]:
        """
        Query GitHub's API for PRs created during the month of October by github_username.

        PRs with an 'invalid' or 'spam' label are ignored

        For PRs created after October 3rd, they have to be in a repository that has a
        'hacktoberfest' topic, unless the PR is labelled 'hacktoberfest-accepted' for it
        to count.

        If PRs are found, return a list of dicts with basic PR information

        For each PR:
            {
            "repo_url": str
            "repo_shortname": str (e.g. "python-discord/seasonalbot")
            "created_at": datetime.datetime
            }

        Otherwise, return None
        """
        logging.info(f"Generating Hacktoberfest PR query for GitHub user: '{github_username}'")
        base_url = "https://api.github.com/search/issues?q="
        not_labels = ("invalid", "spam")
        action_type = "pr"
        is_query = "public"
        not_query = "draft"
        date_range = f"{CURRENT_YEAR}-10-01T00:00:00%2B14:00..{CURRENT_YEAR}-11-01T00:00:00-11:00"
        per_page = "300"
        query_url = (
            f"{base_url}"
            f"-label:{not_labels[0]}"
            f"+-label:{not_labels[1]}"
            f"+type:{action_type}"
            f"+is:{is_query}"
            f"+author:{github_username}"
            f"+-is:{not_query}"
            f"+created:{date_range}"
            f"&per_page={per_page}"
        )
        logging.debug(f"GitHub query URL generated: {query_url}")

        async with aiohttp.ClientSession() as session:
            async with session.get(query_url, headers=REQUEST_HEADERS) as resp:
                jsonresp = await resp.json()

        if "message" in jsonresp.keys():
            # One of the parameters is invalid, short circuit for now
            api_message = jsonresp["errors"][0]["message"]

            # Ignore logging non-existent users or users we do not have permission to see
            if api_message == GITHUB_NONEXISTENT_USER_MESSAGE:
                logging.debug(f"No GitHub user found named '{github_username}'")
            else:
                logging.error(f"GitHub API request for '{github_username}' failed with message: {api_message}")
            return

        if jsonresp["total_count"] == 0:
            # Short circuit if there aren't any PRs
            logging.info(f"No Hacktoberfest PRs found for GitHub user: '{github_username}'")
            return

        logging.info(f"Found {len(jsonresp['items'])} Hacktoberfest PRs for GitHub user: '{github_username}'")
        outlist = []  # list of pr information dicts that will get returned
        oct3 = datetime(int(CURRENT_YEAR), 10, 3, 0, 0, 0)
        for item in jsonresp["items"]:
            shortname = HacktoberStats._get_shortname(item["repository_url"])
            itemdict = {
                "repo_url": f"https://www.github.com/{shortname}",
                "repo_shortname": shortname,
                "created_at": datetime.strptime(
                    item["created_at"], r"%Y-%m-%dT%H:%M:%SZ"
                ),
            }

            # PRs before oct 3 no need to check for topics
            # continue the loop if 'hacktoberfest-accepted' is labeled then
            # there is no need to check for its topics
            if (itemdict["created_at"] < oct3):
                outlist.append(itemdict)
                continue
            if not ("labels" in item.keys()):  # if PR has no labels
                continue
            # checking whether "hacktoberfest-accepted" is one of the PR's labels
            if any(label["name"].casefold() == "hacktoberfest-accepted" for label in item["labels"]):
                outlist.append(itemdict)
                continue

            # fetch topics for the pr repo
            topics_query_url = f"https://api.github.com/repos/{shortname}/topics"
            logging.debug(f"Fetching repo topics for {shortname} with url: {topics_query_url}")
            async with aiohttp.ClientSession() as session:
                async with session.get(topics_query_url, headers=GITHUB_TOPICS_ACCEPT_HEADER) as resp:
                    jsonresp2 = await resp.json()

            if not ("names" in jsonresp2.keys()):
                logging.error(f"Error fetching topics for {shortname}: {jsonresp2['message']}")

            # PRs after oct 3 that doesn't have 'hacktoberfest-accepted' label
            # must be in repo with 'hacktoberfest' topic
            if "hacktoberfest" in jsonresp2["names"]:
                outlist.append(itemdict)
        return outlist

    @staticmethod
    def _get_shortname(in_url: str) -> str:
        """
        Extract shortname from https://api.github.com/repos/* URL.

        e.g. "https://api.github.com/repos/python-discord/seasonalbot"
             |
             V
             "python-discord/seasonalbot"
        """
        exp = r"https?:\/\/api.github.com\/repos\/([/\-\_\.\w]+)"
        return re.findall(exp, in_url)[0]

    @staticmethod
    def _categorize_prs(prs: List[dict]) -> dict:
        """
        Categorize PRs into 'in_review' and 'accepted'.

        PRs created less than 14 days ago are 'in_review', PRs that are not
        are 'accepted' (after 14 days review period).
        """
        now = datetime.now()
        in_review = []
        accepted = []
        for pr in prs:
            if (pr['created_at'] + timedelta(REVIEW_DAYS)) < now:
                in_review.append(pr)
            else:
                accepted.append(pr)

        out_dict = {
            "in_review": in_review,
            "accepted": accepted
        }
        return out_dict

    @staticmethod
    def _build_prs_string(prs: List[tuple], user: str) -> str:
        """
        Builds a discord embed compatible string for a list of PRs.

        Repository name with the link to pull requests authored by 'user' for
        each PR.
        """
        base_url = "https://www.github.com/"
        str_list = []
        repo_list = [pr["repo_shortname"] for pr in prs]
        prs_list = Counter(repo_list).most_common(5)  # get first 5 counted PRs
        more = len(prs) - sum(i[1] for i in prs_list)

        for pr in prs_list:
            # for example: https://www.github.com/python-discord/bot/pulls/octocat
            # will display pull requests authored by octocat.
            # pr[1] is the number of PRs to the repo
            string = f"[{pr[0]}]({base_url}{pr[0]}/pulls/{user})  ({pr[1]})"
            str_list.append(string)
        if more:
            str_list.append(f"...and {more} more")

        return "\n".join(str_list)

    @staticmethod
    def _contributionator(n: int) -> str:
        """Return "contribution" or "contributions" based on the value of n."""
        if n == 1:
            return "contribution"
        else:
            return "contributions"

    @staticmethod
    def _author_mention_from_context(ctx: commands.Context) -> Tuple:
        """Return stringified Message author ID and mentionable string from commands.Context."""
        author_id = str(ctx.message.author.id)
        author_mention = ctx.message.author.mention

        return author_id, author_mention


def setup(bot: commands.Bot) -> None:
    """Hacktoberstats Cog load."""
    bot.add_cog(HacktoberStats(bot))
