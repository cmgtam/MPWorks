import json
import os
import datetime
import logging
from pymongo import MongoClient
import gridfs
from matgendb.creator import VaspToDbTaskDrone
from mpworks.drones.signals import VASPInputsExistSignal, \
    VASPOutputsExistSignal, VASPOutSignal, HitAMemberSignal, SegFaultSignal, \
    VASPStartedCompletedSignal, WallTimeSignal, DiskSpaceExceededSignal, \
    SignalDetectorList
from mpworks.snl_utils.snl_mongo import SNLMongoAdapter
from pymatgen.core.structure import Structure
from pymatgen.matproj.snl import StructureNL

__author__ = 'Anubhav Jain'
__copyright__ = 'Copyright 2013, The Materials Project'
__version__ = '0.1'
__maintainer__ = 'Anubhav Jain'
__email__ = 'ajain@lbl.gov'
__date__ = 'Mar 26, 2013'

logger = logging.getLogger(__name__)


def is_valid_vasp_dir(mydir):
    # note that the OUTCAR and POSCAR are known to be empty in some
    # situations
    files = ["OUTCAR", "POSCAR", "INCAR", "KPOINTS"]
    for f in files:
        m_file = os.path.join(mydir, f)
        if not (os.path.exists(m_file) and os.stat(m_file).st_size > 0):
            return False
    return True


class MPVaspDrone(VaspToDbTaskDrone):
    def assimilate(self, path):
        """
        Parses vasp runs. Then insert the result into the db. and return the
        task_id or doc of the insertion.

        Returns:
            If in simulate_mode, the entire doc is returned for debugging
            purposes. Else, only the task_id of the inserted doc is returned.
        """

        d = self.get_task_doc(path, self.parse_dos,
                              self.additional_fields)
        if not self.simulate:
            # Perform actual insertion into db. Because db connections cannot
            # be pickled, every insertion needs to create a new connection
            # to the db.
            conn = MongoClient(self.host, self.port)
            db = conn[self.database]
            if self.user:
                db.authenticate(self.user, self.password)
            coll = db[self.collection]

            # Insert dos data into gridfs and then remove it from the dict.
            # DOS data tends to be above the 4Mb limit for mongo docs. A ref
            # to the dos file is in the dos_fs_id.
            result = coll.find_one({"dir_name": d["dir_name"]},
                                   fields=["dir_name", "task_id"])
            if result is None or self.update_duplicates:
                if self.parse_dos and "calculations" in d:
                    for calc in d["calculations"]:
                        if "dos" in calc:
                            dos = json.dumps(calc["dos"])
                            fs = gridfs.GridFS(db, "dos_fs")
                            dosid = fs.put(dos)
                            calc["dos_fs_id"] = dosid
                            del calc["dos"]

                d["last_updated"] = datetime.datetime.today()
                if result is None:
                    if ("task_id" not in d) or (not d["task_id"]):
                        d["task_id"] = db.counter.find_and_modify(
                            query={"_id": "taskid"},
                            update={"$inc": {"c": 1}}
                        )["c"]
                    logger.info("Inserting {} with taskid = {}"
                                .format(d["dir_name"], d["task_id"]))
                elif self.update_duplicates:
                    d["task_id"] = result["task_id"]
                    logger.info("Updating {} with taskid = {}"
                                .format(d["dir_name"], d["task_id"]))

                #Fireworks processing
                self.process_fw(path, d)
                coll.update({"dir_name": d["dir_name"]}, {"$set": d},
                            upsert=True)
                return d["task_id"], d
            else:
                logger.info("Skipping duplicate {}".format(d["dir_name"]))
        else:
            d["task_id"] = 0
            logger.info("Simulated insert into database for {} with task_id {}"
                        .format(d["dir_name"], d["task_id"]))
            return 0, d

    def process_fw(self, dir_name, d):
        # custom Materials Project post-processing for FireWorks
        with open(os.path.join(dir_name, 'FW.json')) as f:
            fw_dict = json.load(f)
            d['fw_id'] = fw_dict['fw_id']
            d['snl'] = fw_dict['spec']['mpsnl']
            d['snlgroup_id'] = fw_dict['spec']['snlgroup_id']
            d['submission_id'] = fw_dict['spec'].get('submission_id')
            d['run_tags'] = fw_dict['spec'].get('run_tags', [])
            d['vaspinputset_name'] = fw_dict['spec'].get('vaspinputset_name')
            d['task_type'] = fw_dict['spec']['task_type']

            if 'optimize structure' in d['task_type'] and 'output' in d:
                # create a new SNL based on optimized structure
                new_s = Structure.from_dict(d['output']['crystal'])
                old_snl = StructureNL.from_dict(d['snl'])
                history = old_snl.history
                history.append(
                    {'name': 'Materials Project structure optimization',
                     'url': 'http://www.materialsproject.org',
                     'description': {'task_type': d['task_type'],
                                     'fw_id': d['fw_id'],
                                     'task_id': d['task_id']}})
                new_snl = StructureNL(new_s, old_snl.authors, old_snl.projects,
                                      old_snl.references, old_snl.remarks,
                                      old_snl.data, history)

                # enter new SNL into SNL db
                # get the SNL mongo adapter
                sma = SNLMongoAdapter.auto_load()

                # add snl
                mpsnl, snlgroup_id = sma.add_snl(new_snl)
                d['snl_final'] = mpsnl.to_dict
                d['snlgroup_id_final'] = snlgroup_id
                d['snlgroup_changed'] = (d['snlgroup_id'] !=
                                         d['snlgroup_id_final'])

        # custom processing for detecting errors
        new_style = os.path.exists(os.path.join(dir_name, 'FW.json'))
        vasp_signals = {}
        critical_errors = ["INPUTS_DONT_EXIST",
                           "OUTPUTS_DONT_EXIST", "INCOHERENT_POTCARS",
                           "VASP_HASNT_STARTED", "VASP_HASNT_COMPLETED",
                           "CHARGE_UNCONVERGED", "NETWORK_QUIESCED",
                           "HARD_KILLED", "WALLTIME_EXCEEDED",
                           "ATOMS_TOO_CLOSE", "DISK_SPACE_EXCEEDED"]

        last_relax_dir = dir_name

        if not new_style:
            # get the last relaxation dir
            # the order is relax2, current dir, then relax1. This is because
            # after completing relax1, the job happens in the current dir.
            # Finally, it gets moved to relax2.
            # There are some weird cases where both the current dir and relax2
            # contain data. The relax2 is good, but the current dir is bad.
            if is_valid_vasp_dir(os.path.join(dir_name, "relax2")):
                last_relax_dir = os.path.join(dir_name, "relax2")
            elif is_valid_vasp_dir(dir_name):
                pass
            elif is_valid_vasp_dir(os.path.join(dir_name, "relax1")):
                last_relax_dir = os.path.join(dir_name, "relax1")

        vasp_signals['last_relax_dir'] = last_relax_dir
        ## see what error signals are present

        print "getting signals for dir :{}".format(last_relax_dir)

        sl = SignalDetectorList()
        sl.append(VASPInputsExistSignal())
        sl.append(VASPOutputsExistSignal())
        sl.append(VASPOutSignal())
        sl.append(HitAMemberSignal())
        sl.append(SegFaultSignal())
        sl.append(VASPStartedCompletedSignal())

        signals = sl.detect_all(last_relax_dir)

        signals = signals.union(WallTimeSignal().detect(dir_name))
        if not new_style:
            root_dir = os.path.dirname(dir_name)  # one level above dir_name
            signals = signals.union(WallTimeSignal().detect(root_dir))

        signals = signals.union(DiskSpaceExceededSignal().detect(dir_name))
        if not new_style:
            root_dir = os.path.dirname(dir_name)  # one level above dir_name
            signals = signals.union(DiskSpaceExceededSignal().detect(root_dir))

        signals = list(signals)

        critical_signals = [val for val in signals if val in critical_errors]

        vasp_signals['signals'] = signals
        vasp_signals['critical_signals'] = critical_signals

        vasp_signals['num_signals'] = len(signals)
        vasp_signals['num_critical'] = len(critical_signals)

        if len(critical_signals) > 0 and d['state'] == "successful":
            d["state"] = "error"

        d['vasp_signals'] = vasp_signals