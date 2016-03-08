import PythonQt
from PythonQt import QtCore, QtGui, QtUiTools
import director.applogic as app
import director.objectmodel as om
from director.timercallback import TimerCallback
from director import robotstate
from director import visualization as vis
from director import transformUtils
from director import ikplanner
from director import footstepsdriver
from director import vtkAll as vtk
from director import drcargs
from director import affordanceurdf
from director.roboturdf import HandFactory
from director import lcmUtils
import drc as lcmdrc

import functools
import math
import numpy as np
import types
import lcm
from bot_core.pose_t import pose_t

from director import propertyset
from director.debugVis import DebugData
from director.pointpicker import PlacerWidget
from director import segmentation
from director import filterUtils
from director import vtkNumpy as vnp


def addWidgetsToDict(widgets, d):

    for widget in widgets:
        if widget.objectName:
            d[str(widget.objectName)] = widget
        addWidgetsToDict(widget.children(), d)

class WidgetDict(object):

    def __init__(self, widgets):
        addWidgetsToDict(widgets, self.__dict__)
        
class MotionPlanningPanel(object):
    def __init__(self, robotStateModel, robotStateJointController, teleopRobotModel, teleopJointController, ikPlanner, manipPlanner, affordanceManager, showPlanFunction, hidePlanFunction, footDriver):
        self.robotStateModel = robotStateModel
        self.robotStateJointController = robotStateJointController
        self.teleopRobotModel = teleopRobotModel
        self.teleopJointController = teleopJointController
        self.ikPlanner = ikPlanner
        self.manipPlanner = manipPlanner
        self.affordanceManager = affordanceManager
        self.showPlanFunction = showPlanFunction
        self.hidePlanFunction = hidePlanFunction
        self.footDriver = footDriver
        
        loader = QtUiTools.QUiLoader()
        uifile = QtCore.QFile(':/ui/ddMotionPlanningPanel.ui')
        assert uifile.open(uifile.ReadOnly)
        self.widget = loader.load(uifile)
        self.ui = WidgetDict(self.widget.children())
        
        # Check motion planning mode
        self.ui.mpModeButton.connect('clicked()', self.onMotionPlanningMode)

        # End-pose planning
        self.ui.fpPlannerCombo.connect('currentIndexChanged(const QString&)', self.onPlannerChanged)
        self.ui.handCombo.connect('currentIndexChanged(const QString&)', self.onHandChanged)
        self.ui.baseComboBox.connect('currentIndexChanged(const QString&)', self.onBaseConstraintChanged)
        self.ui.backComboBox.connect('currentIndexChanged(const QString&)', self.onBackConstraintChanged)
        self.ui.feetComboBox.connect('currentIndexChanged(const QString&)', self.onFeetConstraintChanged)
        self.ui.otherHandComboBox.connect('currentIndexChanged(const QString&)', self.onOtherHandConstraintChanged)

        self.ui.fpButton.connect('clicked()', self.onSearchFinalPose)
                
        if 'kneeJointLimits' in drcargs.getDirectorConfig():
            self.kneeJointLimits = drcargs.getDirectorConfig()['kneeJointLimits']
        self.constraintSet = None
        self.palmOffsetDistance = 0.0        
        
        # Foot step planning
        self.placer = None
        self.ui.walkingPlanButton.connect('clicked()', self.onWalkingPlan)
        
        # Motion Planning
        self.ui.motionPlanButton.connect('clicked()', self.onMotionPlan)
        
    def onMotionPlanningMode(self):
        if self.ui.mpModeButton.checked:
            self.activate()
        else:
            self.deactivate()
        
    def activate(self):
        self.ui.mpModeButton.blockSignals(True)
        self.ui.mpModeButton.checked = True
        self.ui.mpModeButton.blockSignals(False)
        self.ui.EndPosePlanningPanel.setEnabled(True)
        self.ui.walkingPlanningPanel.setEnabled(True)
        self.ui.fixFeetPlanningPanel.setEnabled(True)
        self.createHandGoalFrame()
        self.updateIKConstraints()
        
    def deactivate(self):
        self.ui.mpModeButton.blockSignals(True)
        self.ui.mpModeButton.checked = False
        self.ui.mpModeButton.blockSignals(False)
        self.ui.EndPosePlanningPanel.setEnabled(False)
        self.ui.walkingPlanningPanel.setEnabled(False)
        self.ui.fixFeetPlanningPanel.setEnabled(False)
        self.removePlanFolder()
        self.hideTeleopModel()
        
    def onPlannerChanged(self, name):
        assert len(name)
            
    def onHandChanged(self, combo):
        if self.ui.mpModeButton.checked:
            self.createHandGoalFrame()
            self.updateIKConstraints()
        
    def onBaseConstraintChanged(self):
        if self.getComboText(self.ui.baseComboBox) == 'Fixed':
            self.ui.feetComboBox.setCurrentIndex(1)
            self.ui.feetComboBox.show()
            self.removeWalkingPlanningInfo()
        self.updateIKConstraints()
        if self.ui.fpInteractiveCheck.checked:
            self.updateIk()
    
    def onBackConstraintChanged(self):
        self.updateIKConstraints()
        if self.ui.fpInteractiveCheck.checked:
            self.updateIk()
        
    def onFeetConstraintChanged(self):
        if self.getComboText(self.ui.feetComboBox) == 'Fixed': 
            self.ui.walkingPlanningPanel.setEnabled(False)
            self.removeWalkingPlanningInfo()
        elif self.getComboText(self.ui.feetComboBox) == 'Sliding': 
            self.ui.walkingPlanningPanel.setEnabled(True)
            self.ui.baseComboBox.setCurrentIndex(0)
            self.ui.baseComboBox.show()
        self.updateIKConstraints()
        if self.ui.fpInteractiveCheck.checked:
            self.updateIk()
    
    def onOtherHandConstraintChanged(self):
        self.updateIKConstraints()
        if self.ui.fpInteractiveCheck.checked:
            self.updateIk()
        
    def getComboText(self, combo):
        return str(combo.currentText)

    def getReachHand(self):
        return self.getComboText(self.ui.handCombo)

    @staticmethod
    def getGoalFrame(linkName):
        return om.findObjectByName('%s constraint frame' % linkName)
    
    def updateIKConstraints(self):
        startPoseName = 'reach_start'
        startPose = np.array(self.robotStateJointController.q)
        self.ikPlanner.addPose(startPose, startPoseName)

        constraints = []
        constraints.append(self.ikPlanner.createQuasiStaticConstraint())
        constraints.append(self.ikPlanner.createLockedNeckPostureConstraint(startPoseName))
        
        # Get base constraint
        if self.getComboText(self.ui.baseComboBox) == 'Fixed':
            constraints.append(self.ikPlanner.createLockedBasePostureConstraint(startPoseName, lockLegs=False))
            self.ikPlanner.setBaseLocked(True)
        elif self.getComboText(self.ui.baseComboBox) == 'XYZ only':
            constraints.append(self.ikPlanner.createXYZMovingBasePostureConstraint(startPoseName))
            constraints.append(self.ikPlanner.createKneePostureConstraint(self.kneeJointLimits))
        elif self.getComboText(self.ui.baseComboBox) == 'Limited':
            constraints.append(self.ikPlanner.createMovingBaseSafeLimitsConstraint())
            constraints.append(self.ikPlanner.createKneePostureConstraint(self.kneeJointLimits))
            self.ikPlanner.setBaseLocked(False)
            
        # Get back constraint 
        if self.getComboText(self.ui.backComboBox) == 'Fixed':
            constraints.append(self.ikPlanner.createLockedBackPostureConstraint(startPoseName))
            self.ikPlanner.setBackLocked(True)
        elif self.getComboText(self.ui.backComboBox) == 'Limited':
            constraints.append(self.ikPlanner.createMovingBackLimitedPostureConstraint())
            self.ikPlanner.setBackLocked(False)
            
        # Get feet constraint
        if self.getComboText(self.ui.feetComboBox) == 'Fixed':                
            constraints.append(self.ikPlanner.createFixedLinkConstraints(startPoseName, self.ikPlanner.leftFootLink, tspan=[0.0, 1.0], lowerBound=-0.0001*np.ones(3), upperBound=0.0001*np.ones(3), angleToleranceInDegrees=0.1))
            constraints.append(self.ikPlanner.createFixedLinkConstraints(startPoseName, self.ikPlanner.rightFootLink, tspan=[0.0, 1.0], lowerBound=-0.0001*np.ones(3), upperBound=0.0001*np.ones(3), angleToleranceInDegrees=0.1))
        elif self.getComboText(self.ui.feetComboBox) == 'Sliding':
            constraints.extend(self.ikPlanner.createSlidingFootConstraints(startPoseName)[:2])
            constraints.extend(self.ikPlanner.createSlidingFootConstraints(startPoseName)[2:])
                
        if self.getReachHand() == 'Left':
            side = 'left'
            other_side = 'right'
            endEffectorName = self.ikPlanner.handModels[0].handLinkName
        elif self.getReachHand() == 'Right':
            side = 'right'
            other_side = 'left'
            endEffectorName = self.ikPlanner.handModels[1].handLinkName
            
        self.ikPlanner.setArmLocked(side, False)
        if self.getComboText(self.ui.otherHandComboBox) == 'Fixed':
            self.ikPlanner.setArmLocked(other_side, True)
            if other_side == 'left':
                constraints.append(self.ikPlanner.createLockedLeftArmPostureConstraint(startPoseName))
            elif other_side == 'right':
                constraints.append(self.ikPlanner.createLockedRightArmPostureConstraint(startPoseName))
        elif self.getComboText(self.ui.otherHandComboBox) == 'Free':
            self.ikPlanner.setArmLocked(other_side, False)
        
                
        linkName = self.ikPlanner.getHandLink(side)
        graspToHand = self.ikPlanner.newPalmOffsetGraspToHandFrame(side, self.palmOffsetDistance)
        graspToWorld = self.getGoalFrame(linkName)
        p, q = self.ikPlanner.createPositionOrientationGraspConstraints(side, graspToWorld, graspToHand)
        p.tspan = [1.0, 1.0]
        q.tspan = [1.0, 1.0]
        constraints.extend([p, q])
        constraints.append(self.ikPlanner.createActiveEndEffectorConstraint(endEffectorName, self.ikPlanner.getPalmPoint(side)))
        self.constraintSet = ikplanner.ConstraintSet(self.ikPlanner, constraints, 'reach_end', startPoseName)
        
    @staticmethod
    def removePlanFolder():
        om.removeFromObjectModel(om.findObjectByName('teleop plan'))
        om.removeFromObjectModel(om.findObjectByName('walking goal'))
        om.removeFromObjectModel(om.findObjectByName('footstep plan'))
        om.removeFromObjectModel(om.findObjectByName('iDRMStancePoses'))

    @staticmethod
    def removeWalkingPlanningInfo():
        om.removeFromObjectModel(om.findObjectByName('walking goal'))
        om.removeFromObjectModel(om.findObjectByName('footstep plan'))
        om.removeFromObjectModel(om.findObjectByName('iDRMStancePoses'))
        
    @staticmethod
    def getConstraintFrameFolder():
        return om.getOrCreateContainer('constraint frames', parentObj=om.getOrCreateContainer('teleop plan', parentObj=om.findObjectByName('planning')))
    
    def removeHandFrames(self):
        linkName = self.ikPlanner.getHandLink('left')
        frameName = '%s constraint frame' % linkName
        om.removeFromObjectModel(om.findObjectByName(frameName))
        linkName = self.ikPlanner.getHandLink('right')
        frameName = '%s constraint frame' % linkName
        om.removeFromObjectModel(om.findObjectByName(frameName))
        
    def createHandGoalFrame(self):
        if self.getReachHand() == 'Left':
            side = 'left'
        elif self.getReachHand() == 'Right':
            side = 'right'
        else:
            side = 'none'
        self.removeHandFrames()
        
        if not side == 'none':
            folder = self.getConstraintFrameFolder()
            startPose = np.array(self.robotStateJointController.q)
            linkName = self.ikPlanner.getHandLink(side)
            frameName = '%s constraint frame' % linkName
            graspToHand = self.ikPlanner.newPalmOffsetGraspToHandFrame(side, self.palmOffsetDistance)
            graspToWorld = self.ikPlanner.newGraspToWorldFrame(startPose, side, graspToHand)
            frame = vis.showFrame(graspToWorld, frameName, parent=folder, scale=0.2)
            frame.connectFrameModified(self.onGoalFrameModified)
        
    def updateIk(self):
        if not self.constraintSet:
            self.updateIKConstraints()
        endPose, info = self.constraintSet.runIk()
        endPoseName = 'reach_end'
        self.ikPlanner.addPose(endPose, endPoseName)
        self.showPose(self.constraintSet.endPose)
        app.displaySnoptInfo(info)
        
        if self.ui.walkingPlanningPanel.enabled and self.ui.walkInteractiveCheck.checked:
            self.onWalkingPlan()
            
    def onSearchFinalPose(self):
        self.updateIk()
        
    def onGoalFrameModified(self, frame):
        if self.ui.fpInteractiveCheck.checked:
            self.updateIk()
            
    def showPose(self, pose):
        self.teleopJointController.setPose('MP_EndPose', pose)
        self.hidePlanFunction()
        self.showMPModel()
        
    def showMPModel(self):
        self.teleopRobotModel.setProperty('Visible', True)
        self.robotStateModel.setProperty('Visible', True)
        self.robotStateModel.setProperty('Alpha', 0.1)
        
        
    def getCurrentWalkingGoal(self):
        t = self.footDriver.getFeetMidPoint(self.teleopRobotModel)
        t.PreMultiply()
        t.Translate(0.0, 0.0, 0.0)
        t.PostMultiply()
        return t
    
    def onWalkingPlan(self):
        walkingGoal = self.getCurrentWalkingGoal();
        
        frameObj = vis.updateFrame(walkingGoal, 'walking goal', parent='planning', scale=0.25)
        frameObj.setProperty('Edit', False)
        rep = frameObj.widget.GetRepresentation()
        rep.SetTranslateAxisEnabled(2, False)
        rep.SetRotateAxisEnabled(0, False)
        rep.SetRotateAxisEnabled(1, False)
        frameObj.widget.HandleRotationEnabledOff()

        if self.placer:
            self.placer.stop()

        terrain = om.findObjectByName('HEIGHT_MAP_SCENE')
        if terrain:

            pos = np.array(frameObj.transform.GetPosition())

            polyData = filterUtils.removeNonFinitePoints(terrain.polyData)
            if polyData.GetNumberOfPoints():
                polyData = segmentation.labelDistanceToLine(polyData, pos, pos+[0,0,1])
                polyData = segmentation.thresholdPoints(polyData, 'distance_to_line', [0.0, 0.1])
                if polyData.GetNumberOfPoints():
                    pos[2] = np.nanmax(vnp.getNumpyFromVtk(polyData, 'Points')[:,2])
                    frameObj.transform.Translate(pos - np.array(frameObj.transform.GetPosition()))

            d = DebugData()
            d.addSphere((0,0,0), radius=0.03)
            handle = vis.showPolyData(d.getPolyData(), 'walking goal terrain handle', parent=frameObj, visible=True, color=[1,1,0])
            handle.actor.SetUserTransform(frameObj.transform)
            self.placer = PlacerWidget(app.getCurrentRenderView(), handle, terrain)

            def onFramePropertyModified(propertySet, propertyName):
                if propertyName == 'Edit':
                    if propertySet.getProperty(propertyName):
                        self.placer.start()
                    else:
                        self.placer.stop()

            frameObj.properties.connectPropertyChanged(onFramePropertyModified)
            onFramePropertyModified(frameObj, 'Edit')

        frameObj.connectFrameModified(self.onWalkingGoalModified)
        self.onWalkingGoalModified(frameObj)
        
    def onWalkingGoalModified(self, frame):
        om.removeFromObjectModel(om.findObjectByName('footstep widget'))
        request = self.footDriver.constructFootstepPlanRequest(self.robotStateJointController.q, frame.transform)
        self.footDriver.sendFootstepPlanRequest(request)
        
    def onMotionPlan(self):
        startPoseName = 'reach_start'
        startPose = np.array(self.robotStateJointController.q)
        self.ikPlanner.addPose(startPose, startPoseName)
        plan = self.constraintSet.runIkTraj()
        self.showPlan(plan)
        
    def hideTeleopModel(self):
        self.teleopRobotModel.setProperty('Visible', False)
        self.robotStateModel.setProperty('Visible', True)
        self.robotStateModel.setProperty('Alpha', 1.0)
        
    def showPlan(self, plan):
        self.hideTeleopModel()
        self.showPlanFunction(plan)
def _getAction():
    return app.getToolBarActions()['ActionMotionPlanningPanel']

def init(robotStateModel, robotStateJointController, teleopRobotModel, teleopJointController, debrisPlanner, manipPlanner, affordanceManager, showPlanFunction, hidePlanFunction, footDriver):

    global panel
    global dock

    panel = MotionPlanningPanel(robotStateModel, robotStateJointController, teleopRobotModel, teleopJointController, debrisPlanner, manipPlanner, affordanceManager, showPlanFunction, hidePlanFunction, footDriver)
    panel.deactivate()
    dock = app.addWidgetToDock(panel.widget, action=_getAction())
    dock.hide()

    return panel
