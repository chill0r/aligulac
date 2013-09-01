from django import forms
from django.db.models import Q, Sum, Count
from django.shortcuts import render_to_response, get_object_or_404
from django.views.decorators.csrf import csrf_protect

from aligulac.cache import cache_page
from aligulac.tools import get_param, base_ctx, StrippedCharField, generate_messages, Message

from ratings.models import Earnings, Group, Match, Player, Rating
from ratings.tools import filter_active, filter_inactive, total_ratings

# {{{ TeamModForm: Form for modifying a teasm.
class TeamModForm(forms.Form):
    name = StrippedCharField(max_length=100, required=True, label='Name')
    akas = forms.CharField(max_length=200, required=False, label='AKAs')
    shortname = StrippedCharField(max_length=100, required=False, label='Name')
    homepage = StrippedCharField(max_length=200, required=False, label='Homepage')
    lp_name = StrippedCharField(max_length=200, required=False, label='Liquipedia title')

    # {{{ Constructor
    def __init__(self, request=None, team=None):
        if request is not None:
            super(TeamModForm, self).__init__(request.POST)
        else:
            super(TeamModForm, self).__init__(initial={
                'name': team.name,
                'akas': ', '.join(team.get_aliases()),
                'shortname': team.shortname,
                'homepage': team.homepage,
                'lp_name': team.lp_name,
            })

        self.label_suffix = ''
    # }}}

    # {{{ update_player: Pushes updates to player, responds with messages
    def update_team(self, team):
        ret = []

        if not self.is_valid():
            ret.append(Message('Entered data was invalid, no changes made.', type=Message.ERROR))
            print(repr(self.errors))
            for field, errors in self.errors.items():
                for error in errors:
                    ret.append(Message(error=error, field=self.fields[field].label))
            return ret

        def update(value, attr, setter, label):
            if value != getattr(team, attr):
                getattr(team, setter)(value)
                ret.append(Message('Changed %s.' % label, type=Message.SUCCESS))

        update(self.cleaned_data['name'], 'name', 'set_name', 'name')
        update(self.cleaned_data['lp_name'], 'lp_name', 'set_lp_name', 'Liquipedia title')
        update(self.cleaned_data['shortname'], 'shortname', 'set_shortname', 'short name')
        update(self.cleaned_data['homepage'], 'homepage', 'set_homepage', 'homepage')

        if team.set_aliases(self.cleaned_data['akas'].split(',')):
            ret.append(Message('Changed aliases.', type=Message.SUCCESS))

        return ret
    # }}}
# }}} 

# {{{ teams view
@cache_page
def teams(request):
    base = base_ctx('Teams', 'Ranking', request)

    all_teams = Group.objects.filter(is_team=True).prefetch_related('groupmembership_set')

    sort = get_param(request, 'sort', 'ak')
    if sort == 'pl':
        active = all_teams.filter(active=True).order_by('-scorepl', '-scoreak')
    else:
        active = all_teams.filter(active=True).order_by('-scoreak', '-scorepl')

    for t in active:
        t.nplayers = sum([1 if m.current and m.playing else 0 for m in t.groupmembership_set.all()])

    inactive = all_teams.filter(active=False).order_by('name')

    base.update({
        'active': active,
        'inactive': inactive,
    })

    return render_to_response('teams.html', base)
# }}}

# {{{ team view
@cache_page
@csrf_protect
def team(request, team_id):
    # {{{ Get team object and base context, generate messages and make changes if needed
    team = get_object_or_404(Group, id=team_id)
    base = base_ctx('Ranking', None, request)

    if request.method == 'POST':
        form = TeamModForm(request)
        base['messages'] += form.update_team(team)
    else:
        form = TeamModForm(team=team)

    base.update({
        'team': team,
        'form': form,
    })
    base['messages'] += generate_messages(team)
    # }}}

    # {{{ Easy statistics
    players = team.groupmembership_set.filter(current=True, playing=True)
    player_ids = players.values('player')
    matches = Match.objects.filter(Q(pla__in=player_ids) | Q(plb__in=player_ids))

    base.update({
        'nplayers': players.count(),
        'nprotoss': players.filter(player__race=Player.P).count(),
        'nterran':  players.filter(player__race=Player.T).count(),
        'nzerg':    players.filter(player__race=Player.Z).count(),
        'earnings': Earnings.objects.filter(player__in=player_ids)\
                            .aggregate(Sum('earnings'))['earnings__sum'],
        'nmatches': matches.count(),
        'noffline': matches.filter(offline=True).count(),
    })
    # }}}

    # {{{ Player lists
    all_members = total_ratings(Rating.objects.filter(
        player__groupmembership__group=team,
        player__groupmembership__current=True,
        player__groupmembership__playing=True,
        period=base['curp']
    )).order_by('-rating')

    base.update({
        'active':       filter_active(all_members),
        'inactive':     filter_inactive(all_members),
        'nonplaying':   team.groupmembership_set.filter(current=True, playing=False)\
                            .select_related('player').order_by('player__tag'),
        'past':         team.groupmembership_set.filter(current=False)\
                            .annotate(null_end=Count('end'))\
                            .select_related('player').order_by('-null_end', '-end', 'player__tag'),
    })

    return render_to_response('team.html', base)
# }}}