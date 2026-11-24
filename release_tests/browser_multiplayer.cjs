/* Optional Playwright check; use only the isolated multiplayer_browser_server.py fixture. */
const {chromium}=require('playwright'),fs=require('fs'),assert=require('assert/strict');
(async()=>{
 const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
 const browser=await chromium.launch({args:['--no-sandbox']});
 try{
  const bootstrap=await browser.newContext();
  const params=new URLSearchParams(fixture.owner_fragment),admin={Authorization:'Bearer '+params.get('access')};
  const heroes=await(await bootstrap.request.get(fixture.base+'/api/characters',{headers:admin})).json();
  const made=await bootstrap.request.post(fixture.base+'/api/games',{headers:{...admin,'Idempotency-Key':require('crypto').randomUUID()},data:{action:'create',scenario:'missing-book',name:'Новый браузерный прогон',party:fixture.players.slice(0,2).map((p,i)=>({name:p.name,hero_id:heroes.heroes[i].id}))}});
  assert.equal(made.status(),200);const game=await made.json();fixture.room=game.room_id;fixture.players=game.players;params.set('room',game.room_id);fixture.owner_fragment=params.toString();await bootstrap.close();
  const contexts=[],pages=[],errors=[];
  const open=async(fragment)=>{const context=await browser.newContext({viewport:{width:390,height:844}});contexts.push(context);const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));await page.goto(fixture.base+'/#'+fragment);await page.locator('#connection-label').getByText('На связи',{exact:true}).waitFor();return page;};
  const owner=await open(fixture.owner_fragment);
  for(const player of fixture.players)pages.push(await open(player.fragment));
  const send=async(page,text)=>{await page.locator('#message-first').fill(text);await page.locator('.first .send-button').click();};
  for(let i=0;i<2;i++){
   assert.match(await pages[i].locator('#player-identity').textContent(),new RegExp(fixture.players[i].name));
   assert.equal(await pages[i].getByRole('link',{name:'Разработчику',exact:true}).isVisible(),false);
  }
  await send(pages[0],'Привет от первого');
  await pages[1].locator('#group-status').waitFor({state:'visible'});
  await pages[1].locator('#message-first').fill('Черновик второго');
  assert.equal(await pages[1].locator('#message-first').isEnabled(),true);
  assert.equal(await pages[1].locator('.first .send-button').isDisabled(),true);
  await pages[1].getByText('Мастер услышал: Привет от первого',{exact:true}).waitFor();
  assert.equal(await pages[1].locator('#message-first').inputValue(),'Черновик второго');
  await send(pages[1],'Беру верёвку');
  await pages[0].getByText('Вы забираете: Верёвка из кладовой.',{exact:false}).waitFor();
  await pages[1].locator('#show-inventory').click();await pages[1].getByText('Верёвка из кладовой',{exact:true}).waitFor();
  assert.match(await pages[1].locator('.character-heading h3').textContent(),new RegExp(fixture.players[1].character_name));
  assert.equal(await pages[1].getByRole('button',{name:'+ Предмет',exact:true}).count(),0);
  await pages[1].locator('#show-chat').click();
  await send(pages[0],'Пробую открыть сундук');
  await pages[0].getByRole('button',{name:'Бросить кости',exact:true}).waitFor();
  await pages[1].locator('.dice-card').waitFor();assert.equal(await pages[1].getByRole('button',{name:'Бросить кости',exact:true}).count(),0);
  await send(pages[1],'Это проверка моего товарища?');await pages[0].getByText('Мастер услышал: Это проверка моего товарища?',{exact:true}).waitFor();
  await pages[0].getByRole('button',{name:'Бросить кости',exact:true}).click();
  await pages[1].getByText('Проверка завершена.',{exact:true}).waitFor();
  await pages[1].reload();await pages[1].getByText('Проверка завершена.',{exact:true}).waitFor();
  await owner.locator('#games-toggle').click();await owner.locator('#invitation-links article').nth(1).waitFor();
  assert.equal(await owner.locator('#invitation-links article').count(),2);
  await owner.locator('#invite-name').fill('Третий игрок');await owner.locator('#invite-add').click();
  await owner.locator('#invitation-links article').nth(2).waitFor();
  const third=await owner.locator('#invitation-links article').nth(2).locator('a').getAttribute('href');
  const thirdFragment=third.split('#')[1];pages.push(await open(thirdFragment));
  const ownerToken=new URLSearchParams(fixture.owner_fragment).get('access');
  const headers={Authorization:'Bearer '+ownerToken};
  const participants=await (await owner.request.get(fixture.base+`/api/rooms/${fixture.room}/participants`,{headers})).json();
  assert.equal(participants.players.length,3);
  await send(pages[1],'Нападаю на хозяйку');await pages[0].getByRole('link',{name:'Открыть бой',exact:true}).waitFor();
  for(const page of pages){await page.locator('#show-battle').click();await page.frameLocator('#combat-frame').locator('#active-name').getByText(/./).waitFor();}
  let battle=await(await owner.request.get(fixture.base+`/api/rooms/${fixture.room}/combat`,{headers})).json();
  assert.equal(battle.fighters.filter(f=>f.team==='players').length,3);
  const current=battle.fighters[battle.turn];assert.equal(current.team,'players');
  const own=participants.players.findIndex(p=>p.character_id===current.character_id);
  for(let i=0;i<pages.length;i++){
    const frame=pages[i].frameLocator('#combat-frame');
    if(i===own)await frame.locator('[data-command=end_turn]').waitFor();
    else assert.equal(await frame.locator('#hero-actions').isVisible(),false);
  }
  await pages[own].frameLocator('#combat-frame').locator('[data-command=end_turn]').click();
  await pages[own].waitForTimeout(2500);
  battle=await(await owner.request.get(fixture.base+`/api/rooms/${fixture.room}/combat`,{headers})).json();
  assert.notEqual(battle.fighters[battle.turn].character_id,current.character_id);
  assert.deepEqual(errors,[]);
  fs.mkdirSync('.local',{recursive:true});await pages[1].screenshot({path:'.local/multiplayer-browser.png',fullPage:true});
  console.log('PASS independent players: shared journal/status, personal inventory/dice, reload, third invite, owned combat turns');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
