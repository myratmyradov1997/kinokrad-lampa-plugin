"""Исполняем реальный конструктор play(): преролл пропущен, следующая серия сохранена."""
import pathlib
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js required')
def test_playlists_skip_preroll_without_losing_next_episode():
    source = (pathlib.Path(__file__).resolve().parents[1] / 'plugin.js').read_text()
    start = source.index('    function play(audio, stream) {')
    stop = source.index('    // ---- навигация', start)
    script = '''
const assert=require('assert');
let currentFile={}, movie={}, detail={url:'title'}, list, initial;
const fileTitle=()=>'',timelineFor=()=>({}),prefs=()=>{},renderBehindPlayer=()=>{};
const playlistFor=()=>initial;
const Lampa={Player:{playlist:x=>list=x,play:element=>{
 assert.ok(element.continue_play && list.indexOf(element)>=0);
 assert.equal(list.filter(x=>x===element).length,1);
},listener:{follow:()=>{}}}};
'''
    script += source[source.index('    function recoveryFor'):source.index('    function playlistFor')] + source[start:stop]
    script += '''
for(initial of [null,[{url:'__current__'},{url:'next'}]]){
 play({url:'current',quality:{'1080p':'current'},label:'RU'},{tracks:[]});
 assert.equal(list.length,initial?2:1);
 if(initial)assert.equal(list[1].url,'next');
}
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_global_noads_preserves_playlist_and_non_ad_responses():
    source = (pathlib.Path(__file__).resolve().parents[1] / 'plugin.js').read_text()
    helper = source[:source.index('// == KinoKrad')]
    script = r'''
const assert=require('assert');
let hook, list=[], follows=0;
global.window=global;
global.Lampa={Player:{listener:{follow:(event,fn)=>{assert.equal(event,'create');hook=fn;follows++}}},PlayerPlaylist:{get:()=>list,set:x=>list=x}};
global.XMLHttpRequest=function(){};
Object.defineProperty(XMLHttpRequest.prototype,'responseText',{configurable:true,get(){return this.body}});
'''
    script += helper + helper
    script += r'''
assert.equal(follows,1,'two installed plugins must not install duplicate handlers');
const data={url:'movie',vast_url:'ad',vast_banner:'ad'};
list=[{url:'movie'},{url:'next'}];hook({data});
assert.equal(list[0],data);assert.equal(list[1].url,'next');
assert.ok(data.continue_play);assert.ok(!data.vast_url && !data.vast_banner);
const next=list[1];next.vast_url='ad-before-next';next.vast_banner='overlay';hook({data:next});
assert.ok(next.continue_play && !next.vast_url && !next.vast_banner);
assert.equal(list.length,2);assert.equal(list[1],next);
const xhr=new XMLHttpRequest();xhr.body='normal';
xhr.responseURL='https://api.example/search';assert.equal(xhr.responseText,'normal');
xhr.responseURL='https://cub.example/api/ad/get/banner';assert.deepEqual(JSON.parse(xhr.responseText),{ad:[]});
xhr.responseURL='https://cub.example/api/ad/get/preroll';assert.deepEqual(JSON.parse(xhr.responseText),{ad:[]});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_failed_4k_recovers_to_1080_then_720():
    source = (pathlib.Path(__file__).resolve().parents[1] / 'plugin.js').read_text()
    start = source.index('    function play(audio, stream) {')
    stop = source.index('    // ---- навигация', start)
    script = '''
const assert=require('assert');let played,list=[];
let currentFile={},movie={},detail={url:'title'};
const fileTitle=()=>'',timelineFor=()=>({}),prefs=()=>{},renderBehindPlayer=()=>{},playlistFor=()=>null,streamQuery=()=>'';
const audio={index:0,label:'RU',url:'uhd',quality:{'2160p':'uhd','1080p':'hd','720p':'sd'}};
const api=(path,done)=>done({audios:[audio]});
const Lampa={Player:{playlist:x=>list=x,play:x=>played=x,listener:{follow:()=>{}}}};
'''
    script += source[source.index('    function recoveryFor'):source.index('    function playlistFor')] + source[start:stop]
    script += '''
play(audio,{tracks:[]});let urls=[];
played.error(played,url=>urls.push(url));played.error(played,url=>urls.push(url));
played.error(played,url=>urls.push(url));assert.deepEqual(urls,['hd','sd']);
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_next_episode_has_its_own_recovery_and_keeps_timeline():
    source = (pathlib.Path(__file__).resolve().parents[1] / 'plugin.js').read_text()
    start = source.index('    function recoveryFor')
    stop = source.index('    function play(audio, stream)')
    script = '''
const assert=require('assert');
let movie={},detail={url:'title'}, calls=[];
const first={file_id:1,season:1,episode:1},second={file_id:2,season:1,episode:2};
const currentSeason={episodes:[{translations:[first]},{translations:[second]}]};
const preferredTranslation=ep=>ep.translations[0],fileTitle=t=>String(t.file_id),timelineFor=t=>({time:t.file_id*100});
const prefs=()=>({}),streamQuery=(file,extra)=>{calls.push([file.file_id,!!extra]);return ''};
const api=(path,done)=>done({audios:[{index:0,url:'fresh-'+calls.length,quality:{'1080p':'hd'},label:'RU'}]});
'''
    script += source[start:stop]
    script += '''
const list=playlistFor(first);let loaded=false;list[1].url(()=>loaded=true);
assert.ok(loaded);assert.equal(typeof list[1].error,'function');
const timeline=list[1].timeline;let recovered;
list[1].error(list[1],url=>recovered=url);
assert.deepEqual(calls,[[2,false],[2,true]]);assert.ok(recovered);
assert.equal(list[1].timeline,timeline);assert.equal(timeline.time,200);
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_backup_next_episode_keeps_provider_audio_and_recovery():
    source = (pathlib.Path(__file__).resolve().parents[1] / 'plugin.js').read_text()
    script = '''
const assert=require('assert');
let movie={},detail={url:'title',embed_url:'embed'},calls=[];
const first={file_id:10,season:1,episode:10},second={file_id:11,season:1,episode:11};
const currentSeason={episodes:[{translations:[first]},{translations:[second]}]};
const preferredTranslation=ep=>ep.translations[0],fileTitle=t=>String(t.file_id),timelineFor=()=>({});
const prefs=()=>({provider:'backup',audio:'Русская дорожка'}),qs=x=>new URLSearchParams(x).toString();
const api=(path,done)=>{calls.push(new URL('http://lampa.local'+path).searchParams);done({audios:[
 {index:0,url:'en',quality:{'720p':'en'},label:'Английская дорожка'},
 {index:1,url:'ru',quality:{'720p':'ru'},label:'Русская дорожка'}]});};
'''
    script += source[source.index('    function streamQuery'):source.index('    function prefetch')]
    script += source[source.index('    function recoveryFor'):source.index('    function play(audio, stream)')]
    script += '''
const list=playlistFor(first);list[1].url(()=>{});
assert.equal(list[1].url,'ru');let recovered;
list[1].error(list[1],url=>recovered=url);assert.equal(recovered,'ru');
assert.equal(calls.length,2);
for (const query of calls) {assert.equal(query.get('provider'),'backup');assert.equal(query.get('episode'),'11');}
assert.ok(calls[1].get('refresh'));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
