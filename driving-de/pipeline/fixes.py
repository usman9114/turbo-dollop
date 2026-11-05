"""Opus review: corrections applied after Haiku's flags and a manual read of Week 1.
Haiku flags rejected as wrong: 215 (German roundabouts ARE anticlockwise), 260 (it does ban all motor vehicles),
286 (official text literally says 'länger als drei Minuten halten'), 276 (1.5 m / 2 m is the §5(4) wording),
277 (cars and buses are exempt per StVO), 278 tip (280/282 end overtaking bans), 211-10, 238."""
FIX = {
 "306": {"tip": "Where the priority road bends at a junction (a plate shows the thick line turning), you must indicate when you follow the bend, and also when you leave it by going straight on. Keep watching side roads: others may miss your priority."},
 "307": {"tip": "Your priority stops at this sign. Slow down before the next junction and look right: without other signs, rechts vor links applies there."},
 "267": {"explanation": "Sign 267 forbids vehicles to enter the road from your direction. You typically meet it at the far end of a one-way street, facing traffic that would drive in against the one-way direction. Exceptions apply only if a Zusatzzeichen allows them (often 'Radverkehr frei')."},
 "276": {"options": {2: "Motor vehicles must not overtake multi-track motor vehicles (cars, vans, lorries), but may still overtake bicycles and solo motorcycles."}},
 "239": {"tip": "As a driver you may cross a Gehweg only to reach a driveway or car park, and then you give way to everyone (§10 StVO). Never park on it unless Z 315 or markings allow it."},
 "278": {"explanation": "Zeichen 278 cancels the speed limit with that number that was signed earlier. The general limits apply again: 50 km/h in built-up areas, 100 km/h for cars outside them (on motorways there is no general limit, 130 km/h is only recommended). It ends only the speed limit, not an overtaking ban or other bans."},
 "340": {"options": {1: "It guides you in your lane. You may cross it, for example to overtake or turn, if you do not endanger anyone."}},
 "255": {"tip": "Cars may pass this sign. Don't confuse it with Zeichen 260 (car and motorcycle pictured), which bans all motor vehicles."},
 "277": {"tip": "As a car driver you are not addressed by this sign. In the symbol the overtaking vehicle is a red lorry; in Z 276 it is a red car, and that one applies to you."},
 "330.1": {"options": {1: "Follow the special motorway rules; only motor vehicles with a design speed of more than 60 km/h may use it."}},
 "294": {"tip": "At a STOP sign, stop with the car stationary in front of the line. If you still cannot see, creep forward slowly and stop again where you can. Rolling stops and stopping on top of the line are typical test mistakes."},
 "1004-30": {"explanation": "This plate tells you that the danger or rule of the sign above starts after the stated distance, here 100 m ahead. Prepare to adapt your driving as you approach that point."},
 "238": {"name_de": "Reitweg (Sonderweg Reiter)"},
 "103-20": {"explanation": "This warning sign (Gefahrzeichen) announces a single curve to the right. It does not set a speed limit, but you must adapt your speed to the curve and conditions and keep to the right. In built-up areas warning signs stand shortly before the hazard, outside them usually 150–250 m before."},
 "237": {"explanation": "Zeichen 237 marks a cycle path (Radweg) reserved for cyclists, who must use it (Benutzungspflicht). Motor vehicles must not drive, stop or park on it."},
 "241": {"tip": "Many foreign drivers confuse this with the shared path (Zeichen 240, no dividing line). For you as a driver: when turning across such a path, expect fast cyclists on their side."},
 "283": {"tip": "Even 'just a second' with hazard lights on to drop someone off is an offence here. The sign is easy to overlook near bus stops and junctions."},
 "432": {"name_de": "Wegweiser zu innerörtlichen Zielen", "name_en": "Direction sign to local destinations (e.g. station)",
   "options": {0: "A railway station is the only destination on this road", 1: "Trains cross the road ahead", 2: "It points the way to a destination in town, here the railway station (Bahnhof)", 3: "Parking is reserved for station users"},
   "answer": 2, "explanation": "Zeichen 432 is a white direction sign for destinations inside a town, such as the station, city centre or a hospital. It is purely informational and does not change priority or any speed limit."},
 "454": {"name_de": "Umleitungswegweiser", "name_en": "Detour direction sign",
   "options": {0: "Follow the arrow while the detour is signposted; it leads around a blocked section."}},
 "262": {"explanation": "The sign prohibits vehicles whose actual mass, including load and passengers, exceeds the stated value. The figure applies to the vehicle as it is right now, not to the registration papers. It is typically used before weak bridges or roads."},
}
def apply(qs):
    by = {q["num"]: q for q in qs}
    for num, f in FIX.items():
        q = by[num]
        for k, v in f.items():
            if k == "options":
                for i, t in v.items(): q["options"][i] = t
            elif k == "answer": q[k] = v
            else: q[k] = v
        q["review"]["opus"] = "fixed"
    return qs
