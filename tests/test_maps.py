from copy import deepcopy
from uuid import uuid4
from iirpg.maps import validate_map, templates
import unittest
import test_scene

class MapTests(unittest.TestCase):
    setUp = test_scene.SceneTests.setUp
    command = test_scene.SceneTests.command
    def test_import_movement_and_restart(self):
        self.command('join')
        document=next(m for m in templates() if m['id']=='tavern')
        state=self.command('load_map',parameters={'map':document})
        self.assertEqual(state['entities'][-1]['position'],[1,5])
        state=self.command('move_cell',parameters={'position':[3,3]})
        self.assertEqual(state['entities'][-1]['position'],[3,3])
        self.command('tip_over',target_id='table')
        state=self.command('take_cover',target_id='table')
        self.assertIsNotNone(state['entities'][-1]['cover'])
        self.command('move_cell',parameters={'position':[3,4]})
        self.assertIsNone(self.scene.read(self.room)['entities'][-1]['cover'])
        before=self.scene.read(self.room)
        for position in ([0,0],[4,3],[10,6],[True,1],[-1,0]):
            with self.assertRaises(ValueError):self.command('move_cell',parameters={'position':position})
            self.assertEqual(self.scene.read(self.room),before)
        from iirpg.scene import SceneStore
        self.assertEqual(SceneStore(self.roster,lambda s:1).read(self.room),before)
    def test_invalid_generated_documents_do_not_change_scene(self):
        doc=templates()[0];before=self.scene.read(self.room)
        bad=[]
        x=deepcopy(doc);x['rows'][0]='##';bad.append(x)
        x=deepcopy(doc);x['zones'][0]['cells'].append(x['zones'][1]['cells'][0]);bad.append(x)
        x=deepcopy(doc);x['objects'][0]['position']=[0,0];bad.append(x)
        x=deepcopy(doc);x['spawn']=[0,0];bad.append(x)
        x=deepcopy(doc);x['extra']='ignored?';bad.append(x)
        for document in bad:
            with self.assertRaises(ValueError):self.command('load_map',parameters={'map':document})
            self.assertEqual(self.scene.read(self.room),before)
    def test_loading_template_cancels_pending_intents_and_is_idempotent(self):
        self.command('join');self.command('propose',text='Стол')
        before=self.scene.read(self.room);key=str(uuid4())
        command={'action':'load_map','revision':before['revision'],'parameters':{'template_id':'forest_camp'}}
        state=self.scene.execute(self.room,command,key)
        self.assertEqual(state['proposals'][0]['status'],'cancelled')
        count=len(self.store.page(self.room)['messages'])
        self.assertEqual(self.scene.execute(self.room,command,key),state)
        self.assertEqual(len(self.store.page(self.room)['messages']),count)

    def test_difficult_terrain_costs_double_movement(self):
        self.command('join')
        document={'schema_version':1,'id':'corridor','name':'Коридор','description':'Проверка трудной местности',
                  'rows':['#######','#.~~~.#','#######'],'spawn':[1,1],
                  'zones':[{'id':'path','name':'Тропа','cells':[[x,1] for x in range(1,6)]}],'objects':[]}
        self.command('load_map',parameters={'map':document})
        with self.assertRaises(ValueError):self.command('move_cell',parameters={'position':[5,1]})
        state=self.command('move_cell',parameters={'position':[3,1]})
        self.assertEqual(state['entities'][-1]['position'],[3,1])
