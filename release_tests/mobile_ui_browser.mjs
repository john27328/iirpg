// Responsive interaction checks use a local server with a stub at the AI boundary.
const {chromium}=await import(process.env.IIRPG_PLAYWRIGHT_MODULE||'playwright');
const browser=await chromium.launch({headless:true});
const base=process.env.IIRPG_TEST_URL;
if(!base)throw Error('Use browser_character_creation.py --check mobile');
function assert(condition,message){if(!condition)throw Error(message);}
async function fits(page,label){
 const sizes=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
 assert(sizes.scroll<=sizes.width+1,`${label}: page overflows ${sizes.scroll}/${sizes.width}`);
}
async function touchTarget(locator,label){
 const box=await locator.boundingBox();assert(box&&box.height>=43&&box.width>=43,`${label}: small touch target`);
}
async function dialogFits(page,label){
 const box=await page.locator('#hero-creator').boundingBox();const size=page.viewportSize();
 assert(box&&box.x>=-1&&box.y>=-1&&box.x+box.width<=size.width+1&&box.y+box.height<=size.height+1,`${label}: dialog outside viewport ${JSON.stringify(box)} / ${JSON.stringify(size)}`);
 const overflow=await page.locator('#hero-creator').evaluate(el=>el.scrollWidth>el.clientWidth+1);
 assert(!overflow,label+': dialog overflows horizontally');
}
try{
 for(const width of [320,390,430,768,1280]){
  const context=await browser.newContext({viewport:{width,height:844},hasTouch:width<500,isMobile:width<500,reducedMotion:'reduce'});
  const page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
  try{
   await page.goto(base);await page.locator('#games-toggle').click();
   await page.locator('#game-scenario option').first().waitFor({state:'attached'});
   await fits(page,width+' menu');await touchTarget(page.locator('#hero-new'),'create hero');
   await touchTarget(page.locator('#game-create-button'),'new game');
   await page.screenshot({path:'.local/ui-menu-'+width+'.png',fullPage:true});
   await page.locator('#hero-new').click();await page.locator('#hero-creator[open]').waitFor();
   await dialogFits(page,width+' creator');
   const form=page.locator('#hero-card-form');
   await form.locator('[name=name]').fill('Нора '+width+' с очень длинным именем исследовательницы');
   await form.locator('[name=class_id]').selectOption('wizard');
   await form.locator('[name=general_feat]').selectOption('fleet');
   await form.locator('[name=description]').fill('Мирная исследовательница, которая сначала договаривается.');
   await form.locator('[name=edicts]').fill('Не бросать друзей.');
   await page.locator('#hero-card-apply').click();
   await page.waitForFunction(()=>document.querySelector('#hero-card-status').textContent.includes('проверена'));
   if(width<=850){
    await touchTarget(page.locator('#creator-tab-chat'),'assistant tab');
    await page.locator('#creator-tab-card').focus();await page.keyboard.press('ArrowRight');
    assert(await page.locator('#creator-tab-chat').getAttribute('aria-selected')==='true','keyboard tabs failed');
    assert(!await form.isVisible(),'card remains visible in mobile assistant pane');
    if(width===390){await page.setViewportSize({width:1280,height:844});await form.waitFor({state:'visible'});await page.setViewportSize({width,height:844});await form.waitFor({state:'hidden'});}
   }
   await page.locator('#hero-creation-chat textarea').fill('Объясни перк. Остальные поля не меняй.');
   await touchTarget(page.locator('#hero-creation-chat .send-button'),'send to assistant');
   await page.locator('#hero-creation-chat .send-button').click();
   await page.getByText('Выбранный перк увеличивает скорость на 5 футов.',{exact:false}).last().waitFor();
   if(width<=850){
    await page.setViewportSize({width,height:440});
    await dialogFits(page,width+' short viewport');
    const send=await page.locator('#hero-creation-chat .send-button').boundingBox();
    assert(send&&send.y>=0&&send.y+send.height<=440,width+': assistant composer clipped');
    const log=await page.locator('#hero-creation-chat .message-list').boundingBox();
    assert(log&&log.height>=30,width+': message list collapsed with short viewport');
    await page.locator('#hero-creator').screenshot({path:'.local/ui-keyboard-'+width+'.png'});
    await page.setViewportSize({width,height:844});
    await page.locator('#creator-tab-card').click();
   }
   assert(await form.locator('[name=edicts]').inputValue()==='Не бросать друзей.','beliefs lost after pane change');
   await page.locator('#hero-creator').screenshot({path:'.local/ui-creator-'+width+'.png'});
   await page.locator('#hero-card-save').click();
   await page.waitForFunction(()=>document.querySelector('#hero-card-status').textContent.includes('сохранён'));
   await page.locator('#hero-creator-close').click();
   await page.locator('#game-scenario').selectOption('campaign:solo-evening');
   await page.locator('#game-name').fill('Очень длинное название приключения на экране '+width);
   await page.locator('#game-create-button').click();
   await page.locator('#evening-adventure').waitFor({state:'visible'});
   await page.locator('#show-inventory').click();
   if(width<=680)assert(!await page.locator('.roster-sidebar').isVisible(),'single hero search wastes mobile inventory space');
   await page.getByRole('button',{name:'Лист персонажа',exact:true}).first().click();
   await page.locator('.sheet-dialog[open]').waitFor();
   assert(!await page.locator('.sheet-edit-tab').isVisible(),'game stats editable');
   await page.screenshot({path:'.local/ui-sheet-'+width+'.png',fullPage:true});
   await page.locator('.sheet-close').click();await fits(page,width+' inventory');
   const item=page.locator('.item-controls select').first();
   await item.selectOption('stowed');
   await page.waitForFunction(()=>!document.querySelector('.roster-search').disabled);
   await page.locator('#show-chat').click();
   const input=page.locator('#chats textarea');await input.fill('Что мне делать дальше?');
   await page.locator('#chats .send-button').scrollIntoViewIfNeeded();
   await touchTarget(page.locator('#chats .send-button'),'game send');
   await page.locator('#chats .send-button').click();
   await page.getByText('Вы можете исследовать мир и выбирать свои действия. Сообщение получено.',{exact:true}).last().waitFor();
   await fits(page,width+' game chat');
   await page.screenshot({path:'.local/ui-game-'+width+'.png'});
   if(width<=680){
    await page.locator('#show-games').click();
    await page.locator('#games-menu').waitFor({state:'visible'});
    assert(await page.locator('#games-toggle').getAttribute('aria-expanded')==='true','menu state not announced');
    await page.locator('#show-chat').click();
    assert(!await page.locator('#games-menu').isVisible(),'game menu remains open after navigation');
   }
   if(width===320||width===390||width===1280){
    await page.locator('#show-battle').click();
    const battle=page.frameLocator('#combat-frame');await battle.locator('.battle-workspace').waitFor();
    await battle.locator('.connection.online').waitFor({state:'attached'});
    const sizes=await battle.locator('html').evaluate(el=>({scroll:el.scrollWidth,width:innerWidth}));
    assert(sizes.scroll<=sizes.width+1,'tactical view overflows frame');
    assert(!await battle.locator('#battle-preparation').isVisible(),'developer setup leaks into game');
    const field=await battle.locator('#intent-text').boundingBox();assert(field&&field.width>=100,'tactical composer input is squeezed');
    await touchTarget(battle.locator('#battle-chat .send-button'),'tactical send');
    assert(await battle.locator('#battle-chat .chat-header').evaluate(header=>{const title=header.querySelector('h2').getBoundingClientRect(),box=header.getBoundingClientRect();return title.top>=box.top&&title.bottom<=box.bottom;}),'tactical chat heading is clipped');
    await page.locator('#show-chat').click();
   }
   if(width===390){
    await page.locator('#show-games').click();await page.locator('#hero-new').click();
    await page.locator('#hero-card-form [name=name]').fill('Черновик после Escape');
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>!document.querySelector('#hero-creator').open);
    assert(!await page.locator('body').evaluate(el=>el.classList.contains('creator-open')),'closing dialog left page locked');
    await page.locator('#hero-library-panel').evaluate(el=>el.open=true);
    await page.getByRole('button',{name:'Продолжить создание: Черновик после Escape',exact:true}).click();
    assert(await page.locator('#hero-card-form [name=name]').inputValue()==='Черновик после Escape','Escape lost partial draft');
    await page.locator('#hero-creator-close').click();
   }
   assert(errors.length===0,errors.join('; '));
   console.log('PASS responsive UI '+width+': menu, creator, AI pane, short viewport, save, readonly sheet, inventory and chat');
  }finally{await context.close();}
 }
}finally{await browser.close();}
