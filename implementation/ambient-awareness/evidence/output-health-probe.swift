import CoreAudio
import Foundation
func deviceString(_ device: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var title = AudioObjectPropertyAddress(mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    let pointer = UnsafeMutablePointer<CFString?>.allocate(capacity: 1)
    pointer.initialize(to: nil)
    defer { pointer.deinitialize(count: 1); pointer.deallocate() }
    var size = UInt32(MemoryLayout<CFString?>.size)
    guard AudioObjectGetPropertyData(device, &title, 0, nil, &size, pointer) == noErr,
          let value = pointer.pointee else { return nil }
    return value as String
}

func deviceName(_ device: AudioDeviceID) -> String? { deviceString(device, kAudioObjectPropertyName) }

var p=AudioObjectPropertyAddress(mSelector:kAudioHardwarePropertyDefaultOutputDevice,mScope:kAudioObjectPropertyScopeGlobal,mElement:kAudioObjectPropertyElementMain)
var device:AudioDeviceID=0
var bytes=UInt32(MemoryLayout<AudioDeviceID>.size)
var result:[String:Any]=[:]
let status=AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),&p,0,nil,&bytes,&device)
result["available"]=status==noErr && device != 0
result["name"]=deviceName(device)
var channels:[[String:Any]]=[]
for channel:UInt32 in [0,1,2] {
 var row:[String:Any]=["channel":channel]
 var a=AudioObjectPropertyAddress(mSelector:kAudioDevicePropertyMute,mScope:kAudioDevicePropertyScopeOutput,mElement:channel)
 var mute:UInt32=0;var size=UInt32(MemoryLayout<UInt32>.size)
 if AudioObjectHasProperty(device,&a),AudioObjectGetPropertyData(device,&a,0,nil,&size,&mute)==noErr { row["muted"]=mute != 0 }
 a.mSelector=kAudioDevicePropertyVolumeScalar
 var volume:Float32=0;size=UInt32(MemoryLayout<Float32>.size)
 if AudioObjectHasProperty(device,&a),AudioObjectGetPropertyData(device,&a,0,nil,&size,&volume)==noErr { row["volume"]=Double(volume) }
 channels.append(row)
}
result["channels"]=channels
let data=try JSONSerialization.data(withJSONObject:result,options:[.prettyPrinted,.sortedKeys])
FileHandle.standardOutput.write(data)
