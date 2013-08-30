from datetime import datetime, date, timedelta
from functools import partial

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_protect
from django.shortcuts import render_to_response, get_object_or_404

from aligulac.cache import cache_page
from aligulac.tools import Message, base_ctx, generate_messages, post_param
from aligulac.settings import INACTIVE_THRESHOLD

from ratings.models import Match, Player
from ratings.tools import PATCHES, total_ratings, ntz, split_matchset, count_winloss_games, display_matches,\
                          filter_flags

from countries import data, transformations

msg_inactive = 'Due to %s\'s lack of recent games, they have been marked as <em>inactive</em> and '\
             + 'removed from the current rating list. Once they play a rated game they will be reinstated.'
msg_nochart =  '%s has no rating chart on account of having played matches in fewer than two periods.'

# {{{ meandate: Rudimentary function for sorting objects with a start and end date.
def meandate(tm):
    if tm.start is not None and tm.end is not None:
        return (tm.start.toordinal() + tm.end.toordinal()) / 2
    elif tm.start is not None:
        return tm.start.toordinal()
    elif tm.end is not None:
        return tm.end.toordinal()
    else:
        return 1000000
# }}}

# {{{ interp_rating: Takes a date and a rating list, and interpolates linearly.
def interp_rating(date, ratings):
    for ind, r in enumerate(ratings):
        if (r.period.end - date).days >= 0:
            try:
                right = (r.period.end - date).days
                left = (date - ratings[ind-1].period.end).days
                return (left*r.bf_rating + right*ratings[ind-1].bf_rating) / (left+right)
            except:
                return r.bf_rating
    return ratings[-1].bf_rating
# }}}

# {{{ PlayerModForm: Form for modifying a player.
class PlayerModForm(forms.Form):
    tag = forms.CharField(max_length=30, required=True, label='Tag')
    name = forms.CharField(max_length=100, required=False, label='Name')
    akas = forms.CharField(max_length=200, label='AKAs')
    birthday = forms.DateField(required=False, label='Birthday')

    tlpd_id = forms.IntegerField(required=False, label='TLPD ID')
    tlpd_db = forms.MultipleChoiceField(
        required=False, 
        choices=[
            (Player.TLPD_DB_WOLBETA,        'WoL Beta'),
            (Player.TLPD_DB_KOREAN,         'WoL Korean'),
            (Player.TLPD_DB_INTERNATIONAL,  'WoL International'),
            (Player.TLPD_DB_HOTSBETA,       'HotS Beta'),
            (Player.TLPD_DB_HOTS,           'HotS'),
        ],
        label='TLPD DB',
        widget=forms.CheckboxSelectMultiple)
    lp_name = forms.CharField(max_length=200, required=False, label='Liquipedia title')
    sc2c_id = forms.IntegerField(required=False, label='SC2Charts.net ID')
    sc2e_id = forms.IntegerField(required=False, label='SC2Earnings.com ID')

    country = forms.ChoiceField(choices=data.countries, required=False, label='Country')

    # {{{ Constructor
    def __init__(self, request=None, player=None):
        if request is not None:
            super(PlayerModForm, self).__init__(request.POST)
        else:
            super(PlayerModForm, self).__init__(initial={
                'tag': player.tag,
                'country': player.country,
                'name': player.name,
                'akas': ', '.join(player.get_aliases()),
                'birthday': player.birthday,
                'sc2c_id': player.sc2c_id,
                'sc2e_id': player.sc2e_id,
                'lp_name': player.lp_name,
                'tlpd_id': player.tlpd_id,
                'tlpd_db': filter_flags(player.tlpd_db),
            })

        self.label_suffix = ''
    # }}}

    # {{{ Cleaning
    def clean_tag(self):
        s = self.cleaned_data['tag'].strip()
        if s == '':
            raise ValidationError('This field is required.')
        return s

    def basic_clean(self, field):
        if self.cleaned_data[field] is None:
            return None
        s = self.cleaned_data[field].strip()
        return s if s != '' else None

    clean_name = lambda self: self.basic_clean('name')
    clean_lp_name = lambda self: self.basic_clean('lp_name')
    # }}}

    # {{{ update_player: Pushes updates to player, responds with messages
    def update_player(self, player):
        ret = []

        if not self.is_valid():
            ret.append(Message('Entered data was invalid, no changes made.', type=Message.ERROR))
            print(repr(self.errors))
            for field, errors in self.errors.items():
                for error in errors:
                    ret.append(Message(error=error, field=self.fields[field].label))
            return ret

        def basic_update(value, attr, setter, label):
            if value != getattr(player, attr):
                getattr(player, setter)(value)
                ret.append(Message('Changed %s.' % label, type=Message.SUCCESS))

        basic_update(self.cleaned_data['tag'], 'tag', 'set_tag', 'tag')
        basic_update(self.cleaned_data['country'], 'country', 'set_country', 'country')
        basic_update(self.cleaned_data['name'], 'name', 'set_name', 'name')
        basic_update(self.cleaned_data['birthday'], 'birthday', 'set_birthday', 'birthday')
        basic_update(self.cleaned_data['tlpd_id'], 'tlpd_id', 'set_tlpd_id', 'TLPD ID')
        basic_update(sum([int(a) for a in self.cleaned_data['tlpd_db']]), 'tlpd_db', 'set_tlpd_db', 'TLPD DBs')
        basic_update(self.cleaned_data['lp_name'], 'lp_name', 'set_lp_name', 'Liquipedia title')
        basic_update(self.cleaned_data['sc2c_id'], 'sc2c_id', 'set_sc2c_id', 'SC2Charts.net ID')
        basic_update(self.cleaned_data['sc2e_id'], 'sc2e_id', 'set_sc2e_id', 'SC2Earnings.com ID')

        if player.set_aliases(self.cleaned_data['akas'].split(',')):
            ret.append(Message('Changed aliases.', type=Message.SUCCESS))

        return ret
    # }}}

    
# }}} 

# {{{ player view
@cache_page
@csrf_protect
def player(request, player_id):
    # {{{ Get player object and base context, generate messages and make changes if needed
    player = get_object_or_404(Player, id=player_id)
    base = base_ctx('Ranking', '%s:' % player.tag, request, context=player)

    if request.method == 'POST':
        form = PlayerModForm(request)
        base['messages'] += form.update_player(player)
    else:
        form = PlayerModForm(player=player)

    base['messages'] += generate_messages(player)
    # }}}

    # {{{ Various easy data
    matches = player.get_matchset()
    matches_a, matches_b = split_matchset(matches, player)
    w_tot_a, l_tot_a = count_winloss_games(matches_a)
    l_tot_b, w_tot_b = count_winloss_games(matches_b)
    w_vp_a, l_vp_a = count_winloss_games(matches_a.filter(rcb=Player.P))
    l_vp_b, w_vp_b = count_winloss_games(matches_b.filter(rca=Player.P))
    w_vt_a, l_vt_a = count_winloss_games(matches_a.filter(rcb=Player.T))
    l_vt_b, w_vt_b = count_winloss_games(matches_b.filter(rca=Player.T))
    w_vz_a, l_vz_a = count_winloss_games(matches_a.filter(rcb=Player.Z))
    l_vz_b, w_vz_b = count_winloss_games(matches_b.filter(rca=Player.Z))

    base.update({
        'player':           player,
        'form':             form,
        'first':            matches.earliest('date'),
        'last':             matches.latest('date'),
        'totalmatches':     matches.count(),
        'offlinematches':   matches.filter(offline=True).count(),
        'aliases':          player.alias_set.all(),
        'earnings':         ntz(player.earnings_set.aggregate(Sum('earnings'))['earnings__sum']),
        'team':             player.get_current_team(),
        'total':            (w_tot_a + w_tot_b, l_tot_a, l_tot_b),
        'vp':               (w_vp_a + w_vp_b, l_vp_a, l_vp_b),
        'vt':               (w_vt_a + w_vt_b, l_vt_a, l_vt_b),
        'vz':               (w_vz_a + w_vz_b, l_vz_a, l_vz_b),
    })

    if player.country is not None:
        base['countryfull'] = transformations.cc_to_cn(player.country)
    # }}}

    # {{{ Recent matches
    matches = player.get_matchset()\
                    .select_related('pla__rating', 'plb__rating', 'period')\
                    .prefetch_related('message_set')\
                    .filter(date__range=(date.today() - timedelta(days=90), date.today()))\
                    .order_by('-date', '-id')[0:10]

    if matches.exists():
        base['matches'] = display_matches(matches, fix_left=player, ratings=True)
    # }}}

    # {{{ Team memberships
    team_memberships = list(player.groupmembership_set.filter(group__is_team=True))
    team_memberships.sort(key=lambda t: t.id, reverse=True)
    team_memberships.sort(key=meandate, reverse=True)
    team_memberships.sort(key=lambda t: t.current, reverse=True)
    base['teammems'] = team_memberships
    # }}}

    # {{{ If the player has at least one rating
    ratings = total_ratings(player.rating_set.filter(period__computed=True))
    if ratings.exists():
        rating = player.get_current_rating()
        base.update({
            'highs': (
                ratings.latest('rating'),
                ratings.latest('tot_vp'),
                ratings.latest('tot_vt'),
                ratings.latest('tot_vz'),
            ),
            'recentchange': player.get_latest_rating_update(),
            'firstrating': ratings.earliest('period'),
            'rating': rating,
        })

        if rating.decay >= INACTIVE_THRESHOLD:
            base['messages'].append(Message(msg_inactive % player.tag, 'Inactive', type=Message.INFO))

        base['charts'] = base['recentchange'].period_id > base['firstrating'].period_id
    else:
        base['messages'].append(Message('%s has no rating yet.' % player.tag, type=Message.INFO))
        base['charts'] = False
    # }}}

    # {{{ If the player has enough games to make a chart
    if base['charts']:
        ratings = total_ratings(player.rating_set.filter(period_id__lte=base['recentchange'].period_id))\
                  .order_by('period')
        base['ratings'] = ratings
        base['patches'] = PATCHES

        # {{{ Add stories and other extra information
        earliest = base['firstrating']
        latest = base['recentchange']

        # Look through team changes
        teampoints = []
        for mem in base['teammems']:
            if mem.start and earliest.period.end < mem.start < latest.period.end:
                teampoints.append({
                    'date': mem.start,
                    'rating': interp_rating(mem.start, ratings),
                    'data': [{'date': mem.start, 'team': mem.group, 'jol': 'joins'}],
                })
            if mem.end and earliest.period.end < mem.end < latest.period.end:
                teampoints.append({
                    'date': mem.end,
                    'rating': interp_rating(mem.end, ratings),
                    'data': [{'date': mem.end, 'team': mem.group, 'jol': 'leaves'}],
                })
        teampoints.sort(key=lambda p: p['date'])

        # Condense if team changes happened within 14 days
        cur = 0
        while cur < len(teampoints) - 1:
            if (teampoints[cur+1]['date'] - teampoints[cur]['date']).days <= 14:
                teampoints[cur]['data'].append(teampoints[cur+1]['data'][0])
                del teampoints[cur+1]
            else:
                cur += 1

        # Sort first by date, then by joined/left
        for point in teampoints:
            point['data'].sort(key=lambda a: a['jol'], reverse=True)
            point['data'].sort(key=lambda a: a['date'])

        # Look through stories
        stories = player.story_set.all()
        for s in stories:
            if earliest.period.start < s.date < latest.period.start:
                s.rating = interp_rating(s.date, ratings)
            else:
                s.skip = True

        base.update({
            'stories': stories,
            'teampoints': teampoints,
        })
        # }}}
    else:
        base['messages'].append(Message(msg_nochart % player.tag, type=Mesage.INFO))
    # }}}

    return render_to_response('player.html', base)
# }}}